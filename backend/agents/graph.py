"""
LangGraph 编排 —— 两层 Loop：Orchestrator ReAct + Synthesis ↔ Critic 对抗。
"""

import asyncio
import logging
import time
from typing import Literal

from langgraph.graph import StateGraph, END

from backend.agents.state import AnalysisState
from backend.agents.orchestrator import run_orchestrator
from backend.agents.technical import run_technical_agent
from backend.agents.onchain import run_onchain_agent
from backend.agents.derivatives import run_derivatives_agent
from backend.agents.sentiment import run_sentiment_agent
from backend.agents.macro import run_macro_agent
from backend.agents.synthesis import run_synthesis
from backend.agents.critic import run_critic, _should_continue_critic_loop
from backend.config import config

logger = logging.getLogger(__name__)


async def _timed_node(name: str, fn, state: AnalysisState) -> AnalysisState:
    """Wrap graph nodes with durable timing metadata."""
    started = time.perf_counter()
    status = "ok"
    error = ""
    try:
        return await fn(state)
    except Exception as exc:
        status = "error"
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        state.setdefault("_execution_timings", []).append({
            "node": name,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "status": status,
            "error": error,
        })


def _make_timed_node(name: str, fn):
    """Return an explicitly async LangGraph node wrapper.

    LangGraph treats a synchronous callable that returns a coroutine as a
    synchronous node and attempts to write the coroutine into the state.  The
    previous inline lambdas had exactly that shape, causing full graph runs to
    fail with ``InvalidUpdateError`` before any Agent work completed.
    """
    async def wrapped(state: AnalysisState) -> AnalysisState:
        return await _timed_node(name, fn, state)

    return wrapped

# Agent 名 → 运行函数映射（受数据源可用性开关控制：禁用的维度不出现在
# 执行映射中，即使 Orchestrator 意外请求也会被跳过）
AGENT_MAP = {
    "technical": run_technical_agent,
    "onchain": run_onchain_agent,
    "derivatives": run_derivatives_agent,
    "sentiment": run_sentiment_agent,
}
if config.ENABLE_MACRO_AGENT:
    AGENT_MAP["macro"] = run_macro_agent


async def _run_agents_parallel(state: AnalysisState) -> AnalysisState:
    """并行执行 Orchestrator 指定的专家 Agent。

    去重：跳过已在 evidence_pool 中存在的 Agent（防止同一 Agent 被重复调用
    产生重复证据，导致 Orchestrator 无限循环和证据膨胀）。
    """
    decision = state.get("orchestrator_decision", {})
    agents_to_call = decision.get("agents", [])

    if not agents_to_call:
        return state

    # 去重：已存在于 evidence_pool 的 Agent 不再重新执行
    evidence = state.get("evidence_pool", [])
    existing_agents = {e.get("agent") for e in evidence}

    tasks = []
    skipped = []
    for agent_name in agents_to_call:
        agent_name = agent_name.strip().lower()
        if agent_name in existing_agents:
            skipped.append(agent_name)
            continue
        if agent_name in AGENT_MAP:
            async def _run_one(name=agent_name, fn=AGENT_MAP[agent_name]):
                started = time.perf_counter()
                status = "ok"
                error = ""
                try:
                    return await fn(state)
                except Exception as exc:
                    status = "error"
                    error = f"{type(exc).__name__}: {exc}"
                    raise
                finally:
                    state.setdefault("_execution_timings", []).append({
                        "node": name,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                        "status": status,
                        "error": error,
                    })
            tasks.append(_run_one())

    if skipped:
        logger.info("跳过 %d 个已采集 Agent: %s", len(skipped), skipped)

    if tasks:
        await asyncio.gather(*tasks)
        state["_no_progress_agents"] = False
    else:
        # 关键修复：Orchestrator 请求的 Agent 全部已存在于证据池中（被去重跳过），
        # 本轮没有产生任何新证据。此前会静默返回，导致下一轮 Orchestrator 仍看到
        # 同样"数据质量不佳"的旧证据，可能再次决定重复调用同一个已采集的 Agent，
        # 形成"Orchestrator→run_agents(空转)→Orchestrator"的死循环，白白消耗步数上限
        # 和大量慢思考 LLM 调用却没有任何进展（即"CORE→DONE 循环"现象的根因之一）。
        # 这里显式标记本轮无新增证据，供 Orchestrator 下一轮早退判断。
        state["_no_progress_agents"] = True
        logger.warning(
            "Orchestrator 请求的 Agent %s 均已采集（无新证据），标记本轮空转，防止死循环", agents_to_call,
        )

    return state


def _should_continue_main_loop(state: AnalysisState) -> Literal["call_agents", "synthesis"]:
    """判断主循环下一步。"""
    decision = state.get("orchestrator_decision", {})
    action = decision.get("action", "synthesize")
    step = state.get("step", 0)
    max_steps = config.MAX_ORCHESTRATOR_STEPS

    if action == "call_agents" and step < max_steps and decision.get("agents"):
        return "call_agents"
    return "synthesis"


def build_graph() -> StateGraph:
    """构建 LangGraph StateGraph。"""
    workflow = StateGraph(AnalysisState)

    # 添加节点
    workflow.add_node("orchestrator", _make_timed_node("orchestrator", run_orchestrator))
    workflow.add_node("run_agents", _make_timed_node("run_agents", _run_agents_parallel))
    workflow.add_node("synthesis", _make_timed_node("synthesis", run_synthesis))
    workflow.add_node("critic", _make_timed_node("critic", run_critic))

    # 设置入口
    workflow.set_entry_point("orchestrator")

    # 条件边：Orchestrator → 调用 Agent 或 Synthesis
    workflow.add_conditional_edges(
        "orchestrator",
        _should_continue_main_loop,
        {
            "call_agents": "run_agents",
            "synthesis": "synthesis",
        },
    )

    # Agent 完成后 → 回到 Orchestrator 评估
    workflow.add_edge("run_agents", "orchestrator")

    # Synthesis → Critic
    workflow.add_edge("synthesis", "critic")

    # Critic → 条件：继续修正 或 完成
    workflow.add_conditional_edges(
        "critic",
        lambda s: _should_continue_critic_loop(s),
        {
            "synthesis_revise": "synthesis",
            "finalize": END,
        },
    )

    return workflow.compile()


# ── 延迟编译单例 ──
# 避免 import 时立即编译 StateGraph，改为首次访问时懒编译。
# 对单元测试、CI 等无需实际运行图的场景无副作用。
_graph: StateGraph | None = None


def get_analysis_graph() -> StateGraph:
    """延迟编译并返回全局 Analysis Graph（线程安全）。"""
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


# 保留向后兼容
analysis_graph = get_analysis_graph()
