"""
Orchestrator Agent —— 任务规划与调度（慢思考）。
负责分析用户意图、决定调用哪些专家、判断证据是否充分。
"""

import logging
from typing import Any

from backend.agents.state import AnalysisState
from backend.config import config
from backend.llm.hy3_client import hy3_client
from backend.utils.json_utils import parse_json

logger = logging.getLogger(__name__)

ORCHESTRATOR_SYSTEM = """你是一个加密货币市场分析系统的任务调度器（Orchestrator）。

你的职责：
1. 分析用户查询，理解其意图（是想了解趋势？寻找进场时机？排查某个事件？）
2. 决定需要调用哪些专家 Agent 来收集数据
3. 评估已有证据是否充分、一致；特别关注信号矛盾检测结果
4. 决定是继续收集数据，还是进入综合研判阶段

可调用的专家 Agent：
- technical: 技术面分析（K线、MACD、RSI、布林带、关键价位）
- onchain: 链上数据（鲸鱼动向、交易所净流入/流出）
- derivatives: 衍生品数据（资金费率、OI、爆仓分布）
- sentiment: 舆情情绪（恐惧贪婪指数、社交媒体情绪）
- macro: 宏观/地缘政治（联网搜索新闻事件）

决策规则：
- 分析质量优先于速度：除非用户查询明确只关心单一维度，否则应尽量调用全部 5 个专家 Agent
  （technical/onchain/derivatives/sentiment/macro）以获得最完整的证据覆盖，不要为了节省时间而遗漏维度
- 首次查询：至少调用 technical + derivatives + sentiment + onchain + macro 全量基线
- 如果证据不足：指定需要补充的 Agent
- ⚠ 信号矛盾处理：若出现维度间矛盾（如 technical看多 vs derivatives看空），
  优先调用未覆盖的 Agent 来破局（如 onchain/macro 作为 tie-breaker），而非直接进入合成
- 如果各维度信号一致且置信度 ≥ {threshold}：进入 synthesis（综合研判）
- 如果步数 ≥ {max_steps}：强制进入 synthesis
- 注意错误标记 ⚠错误 的维度，其信号不应采信，需考虑重试或忽略

请以 JSON 格式输出你的决策：
```json
{{
  "reasoning": "你的推理过程（包含对信号矛盾的判断）",
  "action": "call_agents",  // 或 "synthesize" 或 "done"
  "agents": ["technical", "derivatives"],  // 仅 action=call_agents 时需要
  "observation": "对当前证据状态的评估",
  "contradiction": false  // 是否存在尚未解决的信号矛盾
}}
```""".format(
    threshold=config.MIN_CONFIDENCE_THRESHOLD,
    max_steps=config.MAX_ORCHESTRATOR_STEPS,
)


async def run_orchestrator(state: AnalysisState) -> AnalysisState:
    """Orchestrator 节点：规划下一步动作。

    输入：用户 query + 当前证据池 + 步数
    输出：更新 orchestrator_decision

    早退机制：仅当全部 5 个专家 Agent 均已产出证据、且共识明确（≥4 维同向）时，
    才跳过 LLM 调用直接 synthesize —— 保证质量优先：不会因为部分维度提前一致
    就放弃尚未采集的维度（如 macro/onchain），避免证据覆盖不全。
    """
    step = state.get("step", 0)
    evidence = state.get("evidence_pool", [])
    query = state.get("query", "分析当前市场")

    # 早退 1：五维证据齐全且共识明确 → 跳过 LLM，直接融合
    if _is_evidence_sufficient(evidence) and step > 0:
        logger.info(
            "Orchestrator 早退：五维证据已齐全且共识明确，直接融合",
        )
        decision = {
            "action": "synthesize",
            "agents": [],
            "reasoning": f"已有{len(evidence)}维证据共识明确，无需再调 Agent",
        }
        state["orchestrator_decision"] = decision
        state["step"] = step + 1
        state.setdefault("trace", []).append({
            "node": "orchestrator", "step": step, "decision": decision,
        })
        return state

    # 早退 2：已达最大步数 → 跳过 LLM，直接强制融合
    # 关键修复：此前该检查在 LLM 调用之后才生效，导致哪怕已经到达步数上限，
    # 仍会白白浪费一次 10-25s 的高档慢思考调用（deepseek/hy3 high reasoning）。
    if step >= config.MAX_ORCHESTRATOR_STEPS:
        logger.info("Orchestrator 早退：已达最大步数 %d，跳过 LLM 直接融合", config.MAX_ORCHESTRATOR_STEPS)
        decision = {
            "action": "synthesize",
            "agents": [],
            "reasoning": f"已达最大步数({config.MAX_ORCHESTRATOR_STEPS})，强制进入融合",
        }
        state["orchestrator_decision"] = decision
        state["step"] = step + 1
        state.setdefault("trace", []).append({
            "node": "orchestrator", "step": step, "decision": decision,
        })
        return state

    # 早退 3：上一轮 run_agents 请求的 Agent 全部已在证据池中（被去重跳过，无新证据）
    # → 说明再让 LLM 决策也大概率会重复请求同一批已采集但质量不佳的 Agent（如 SSL
    # 失败降级的 onchain），造成 Orchestrator↔run_agents 空转死循环（此前"CORE→DONE
    # 循环"现象的根因）。直接强制进入融合，用已有证据（即便部分 degraded）产出结果，
    # 好于无限空转直到耗尽步数上限。
    if state.get("_no_progress_agents"):
        logger.warning("Orchestrator 早退：检测到上一轮 Agent 调用空转（无新证据），强制进入融合防止死循环")
        decision = {
            "action": "synthesize",
            "agents": [],
            "reasoning": "上一轮请求的 Agent 均已采集但无新证据，为防止死循环强制进入融合",
        }
        state["orchestrator_decision"] = decision
        state["step"] = step + 1
        state["_no_progress_agents"] = False
        state.setdefault("trace", []).append({
            "node": "orchestrator", "step": step, "decision": decision,
        })
        return state

    # 构建用户消息，包含当前状态
    evidence_summary = _summarize_evidence(evidence)
    user_msg = f"""用户查询：{query}

当前步数：{step}/{config.MAX_ORCHESTRATOR_STEPS}
已有证据：{evidence_summary}

请决定下一步动作。"""

    messages = [
        {"role": "system", "content": ORCHESTRATOR_SYSTEM},
        {"role": "user", "content": user_msg},
    ]

    try:
        response = await hy3_client.achat(
            messages=messages,
            # 不传 model：由 UnifiedClient._resolve_model 根据当前激活的 provider
            # (hy3/deepseek) 自动选择对应的 slow 模型，避免跨 provider 硬编码模型名报错
            reasoning_effort="high",
            temperature=0.3,
        )
        content = response.choices[0].message.content or "{}"

        # 提取 JSON
        decision = parse_json(content, default={
            "action": "synthesize" if evidence else "call_agents",
            "agents": ["technical", "derivatives", "sentiment"],
            "reasoning": "默认启动核心Agent",
        })
        # 注：步数上限检查已提前到函数开头（早退 2），此处无需重复判断

    except Exception as e:
        # 关键修复：LLM 调用失败时不能一刀切地跳过所有数据 Agent。
        # - 若尚无证据（首次调用失败）：仍应尝试默认基线 Agent 组合，而非直接空转合成
        # - 若已有部分证据：才考虑强制融合，避免死循环
        # 同时 graph.py 的 Agent 去重 + MAX_ORCHESTRATOR_STEPS 步数上限已能防止死循环，
        # 这里不必再靠"零 Agent"这种过激手段兜底。
        if not evidence and step == 0:
            logger.warning("Orchestrator 决策失败: %s，首轮调用默认基线 Agent（不放弃采集）", e)
            decision = {
                "action": "call_agents",
                "agents": ["technical", "derivatives", "sentiment"],
                "reasoning": f"LLM调用失败({e})，使用默认基线 Agent 组合以确保有数据可用",
            }
        else:
            logger.warning("Orchestrator 决策失败: %s，已有 %d 维证据，强制进入融合", e, len(evidence))
            decision = {
                "action": "synthesize",
                "agents": [],
                "reasoning": f"LLM调用失败({e})，已有{len(evidence)}维证据，强制进入融合",
            }

    state["orchestrator_decision"] = decision
    state["step"] = step + 1

    # 留痕
    trace_entry = {
        "node": "orchestrator",
        "step": step,
        "decision": decision,
    }
    if "trace" not in state:
        state["trace"] = []
    state["trace"].append(trace_entry)

    return state


ALL_AGENTS = {"technical", "onchain", "derivatives", "sentiment", "macro"}


def _is_evidence_sufficient(evidence: list[dict]) -> bool:
    """判断证据是否充分：要求全部 5 个专家 Agent 均已产出证据（质量优先，
    不因部分维度提前一致就放弃尚未采集的维度），且共识明确（≥4 维同向）时早退；
    否则仍需 LLM 判定是否要补充调用或强制融合。
    """
    covered = {e.get("agent") for e in evidence}
    if not ALL_AGENTS.issubset(covered):
        return False

    # 排除置信度为 0 的纯错误信号
    valid = [e for e in evidence if e.get("confidence", 0) > 0.01]
    if len(valid) < 4:
        return False

    biases = [e.get("bias", "neutral") for e in valid]
    # 同一方向 ≥4 维即视为有共识 → 直接融合，不再调 LLM
    if biases.count("bullish") >= 4 or biases.count("bearish") >= 4:
        return True

    return False


def _summarize_evidence(evidence: list[dict]) -> str:
    """汇总已有证据为结构化文本（含原始关键数值 + 证据详情 + 矛盾检测 + 错误标记）。

    把每个维度的关键原始数值写进摘要，让 Orchestrator 基于事实做决策，
    而非仅看最终 bias/score。
    """
    if not evidence:
        return "（尚无证据）"

    lines = []
    for e in evidence:
        agent = e.get("agent", "unknown")
        bias = e.get("bias", "neutral")
        score = e.get("score", 50)
        conf = e.get("confidence", 0)
        dq = e.get("data_quality", "unknown")
        has_error = conf == 0.0 or dq == "degraded"
        err_tag = " ⚠数据异常" if has_error else ""

        # 维度摘要
        lines.append(
            f"- [{agent}] 偏向:{bias}, 强度:{score}/100, 可信度:{conf:.2f} 数据质:{dq}{err_tag}"
        )

        # 原始关键数值（Orchestrator 的决策依据，而非只看方向）
        raw = e.get("raw_metrics", {}) or {}
        metric_line = _format_raw_metrics(agent, raw)
        if metric_line:
            lines.append(f"    🔢 {metric_line}")

        # 关键证据（取 top 3）
        ev_items = e.get("evidence", []) or []
        for item in ev_items[:3]:
            lines.append(f"    📊 {item}")

        # 风险提示（取 top 2）
        caveats = e.get("caveats", []) or []
        for c in caveats[:2]:
            lines.append(f"    ⚠ {c}")

    # 矛盾检测
    biases = [e.get("bias", "neutral") for e in evidence]
    bulls = biases.count("bullish")
    bears = biases.count("bearish")
    if bulls > 0 and bears > 0:
        bull_agents = [e.get("agent", "?") for e in evidence if e.get("bias") == "bullish"]
        bear_agents = [e.get("agent", "?") for e in evidence if e.get("bias") == "bearish"]
        lines.append(f"\n⚠ 信号矛盾检测：{bulls}维看多({', '.join(bull_agents)}) vs {bears}维看空({', '.join(bear_agents)})")

    # 数据完整性统计
    real_count = sum(1 for e in evidence if e.get("data_quality") == "real")
    total = len(evidence)
    lines.append(f"\n数据完整性：{real_count}/{total} 维度为真实数据")

    return "\n".join(lines)


def _format_raw_metrics(agent: str, raw: dict) -> str:
    """将原始指标格式化为一行可读文本。"""
    if not raw:
        return ""
    parts = []
    if agent == "technical":
        if "rsi" in raw:
            parts.append(f"RSI={raw['rsi']}")
        if "macd_signal" in raw:
            parts.append(f"MACD={raw['macd_signal']}")
        if "close" in raw:
            parts.append(f"现价={raw['close']}")
    elif agent == "onchain":
        if "netflow_signal" in raw:
            parts.append(raw["netflow_signal"])
    elif agent == "derivatives":
        if "funding_rate_pct" in raw:
            parts.append(f"费率={raw['funding_rate_pct']}%")
        if "oi_change_pct" in raw:
            parts.append(f"OI变={raw['oi_change_pct']}%")
        if "dominant_side" in raw:
            parts.append(raw["dominant_side"])
    elif agent == "sentiment":
        if "fgi_value" in raw:
            parts.append(f"FGI={raw['fgi_value']}")
    elif agent == "macro":
        if "event_count" in raw:
            parts.append(f"搜到{raw['event_count']}条新闻")
        if "government_event_count" in raw:
            parts.append(f"政府资产事件={raw['government_event_count']}条")
        if raw.get("freshness_verified"):
            parts.append("发布时间已校验")
        if raw.get("freshest_age_hours") is not None:
            parts.append(f"最新={raw['freshest_age_hours']}小时前")
        if raw.get("oldest_age_hours") is not None:
            parts.append(f"最旧={raw['oldest_age_hours']}小时前")
        if raw.get("search_call_count") is not None:
            parts.append(f"查询={raw['search_call_count']}/{raw.get('search_call_budget', '?')}")
        if "key_events" in raw:
            parts.append(f"事件={raw['key_events']}")
        if raw.get("missing_mandatory_news_queries"):
            parts.append(f"未执行政府查询={len(raw['missing_mandatory_news_queries'])}")
    return " | ".join(parts)
