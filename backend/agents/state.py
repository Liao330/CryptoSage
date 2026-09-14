"""
LangGraph State Schema —— 全局分析状态。
"""

from typing import Any, TypedDict


class Signal(TypedDict, total=False):
    """标准化信号。"""
    agent: str
    symbol: str
    bias: str               # bullish | bearish | neutral
    score: int              # 0-100 看多强度
    confidence: float       # 0-1 可信度
    key_levels: dict
    evidence: list[str]
    caveats: list[str]
    data_source: str        # 实际命中的数据源名（如 "google_news_rss" / "gate" / "blockchain.com"）
    data_quality: str       # "real" | "partial" | "degraded" — 数据源覆盖程度
    raw_metrics: dict       # 原始关键数值（RSI、资金费率、FGI 等），供 Orchestrator 决策和前端溯源


class AnalysisState(TypedDict, total=False):
    """LangGraph 全局状态。"""
    # 输入
    query: str
    symbol: str
    as_of: str | None    # as-of 回测模式：分析所"回到"的历史时刻（ISO 8601）；None=实时

    # Loop 1: 证据池
    evidence_pool: list[dict]          # 各 Agent 产出的标准化信号
    step: int                          # 当前步数
    orchestrator_decision: dict        # {"action": "call_agents"|"synthesize"|"done", "agents": [...]}

    # Loop 2: 对抗
    synthesis_result: dict | None
    critic_round: int
    critic_feedback: list[str]

    # 输出
    final_report: dict | None

    # 全程留痕（前端可视化）
    trace: list[dict]

    # 内部标记：上一轮 run_agents 请求的 Agent 是否全部因去重被跳过（无新证据产出）。
    # 必须在 Schema 中显式声明，否则 LangGraph 不会为其创建状态通道，跨节点写入会丢失
    # （这也是此前 Orchestrator 空转防护逻辑失效的根因）。
    _no_progress_agents: bool

    # Critic 只能对确定性校准后的置信度施加一次、最多 10 个百分点的风险折扣。
    _critic_confidence_calibrated: bool
    _critic_confidence_penalty: float

    # 同一分析内复用历史绩效校准，避免每轮 Critic 修订重复查询数据库。
    _agent_weight_multipliers: dict[str, float]
    _agent_performance: dict

    # Durable execution observability: per-node/per-agent elapsed timings.
    _execution_timings: list[dict]
