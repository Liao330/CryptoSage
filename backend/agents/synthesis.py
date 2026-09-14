"""
Synthesis Agent —— 信号融合（慢思考）。
按市场状态动态加权，综合各维度信号输出初步结论。
"""

import json
import logging

from backend.agents.state import AnalysisState
from backend.calibration.performance import get_dynamic_weight_multipliers
from backend.config import config
from backend.data.news_graph import assess_government_event
from backend.llm.hy3_client import hy3_client
from backend.utils.json_utils import parse_json

logger = logging.getLogger(__name__)

# 宏观维度可用性开关：禁用时 Synthesis 的维度框架/权重矩阵/融合规则中
# 完全不出现宏观概念（LLM 无感知，不会输出"无宏观输入"类降级表述）
_MACRO_ENABLED = config.ENABLE_MACRO_AGENT

_NEUTRAL_SIGNAL_BAND = 0.08
# A single independent signal can be a useful forecast, but it should never
# become an executable high-confidence trade by itself.
_SINGLE_SOURCE_CONFIDENCE_CAP = 0.54
_LIMITED_DATA_CONFIDENCE_CAP = 0.35
_MARKET_WEIGHTS = {
    "default": {"technical": 0.25, "onchain": 0.20, "derivatives": 0.25, "sentiment": 0.15, "macro": 0.15},
    "trend": {"technical": 0.30, "onchain": 0.18, "derivatives": 0.25, "sentiment": 0.12, "macro": 0.15},
    "range": {"technical": 0.15, "onchain": 0.25, "derivatives": 0.25, "sentiment": 0.20, "macro": 0.15},
    "event": {"technical": 0.15, "onchain": 0.15, "derivatives": 0.25, "sentiment": 0.15, "macro": 0.30},
    "geopolitical": {"technical": 0.10, "onchain": 0.15, "derivatives": 0.15, "sentiment": 0.15, "macro": 0.45},
}
_TIMEFRAME_WEIGHTS = {
    "4H": {"technical": 0.35, "onchain": 0.10, "derivatives": 0.30, "sentiment": 0.15, "macro": 0.10},
    "24H": _MARKET_WEIGHTS["default"],
    "7D": {"technical": 0.15, "onchain": 0.25, "derivatives": 0.15, "sentiment": 0.10, "macro": 0.35},
}
_TIMEFRAME_CONTEXT = {
    "4H": {
        "label": "短线执行窗口",
        "focus": "技术动量、资金费率与清算拥挤度",
        "risk": "突发新闻和流动性扫损可能在一个交易日内使技术信号失效",
    },
    "24H": {
        "label": "日内平衡窗口",
        "focus": "多维度信号的平衡与次日事件风险",
        "risk": "宏观数据发布或情绪反转可能改变当前共识",
    },
    "7D": {
        "label": "波段观察窗口",
        "focus": "宏观流动性、链上资金迁移与趋势延续性",
        "risk": "政策、资金流和叙事变化的累积效应可能压过短线形态",
    },
}
_AGENT_LABELS = {
    "technical": "技术面",
    "onchain": "链上",
    "derivatives": "衍生品",
    "sentiment": "舆情",
    "macro": "宏观",
}


def _data_quality_factor(item: dict) -> float:
    """Return a bounded coverage factor for a signal.

    ``real`` means the returned payload is from a live source, not that every
    sub-signal in the Agent was available.  Agents can provide a more precise
    ``raw_metrics.coverage_score`` for partial feeds; otherwise ``partial`` is
    conservatively treated as 0.5 and degraded/unavailable data contributes 0.
    """
    quality = str(item.get("data_quality") or "degraded").lower()
    default = {"real": 1.0, "partial": 0.5}.get(quality, 0.0)
    raw = item.get("raw_metrics") if isinstance(item.get("raw_metrics"), dict) else {}
    try:
        explicit = float(raw.get("coverage_score"))
    except (TypeError, ValueError):
        explicit = default
    factor = explicit if quality in {"real", "partial"} else 0.0
    # Backward-compatible inference for reports created before partial quality
    # metadata was added.
    if str(item.get("agent")) == "derivatives":
        options = item.get("options_snapshot") if isinstance(item.get("options_snapshot"), dict) else {}
        if options.get("data_quality") not in (None, "real"):
            factor = min(factor, 0.60)
    if str(item.get("agent")) == "onchain":
        netflow_signal = str(raw.get("netflow_signal") or "")
        if "近似" in netflow_signal or "不能等同" in " ".join(map(str, item.get("caveats", []) or [])):
            factor = min(factor, 0.35)
    if str(item.get("agent")) == "macro" and raw.get("source_quality_score") is not None:
        try:
            factor = min(factor, max(0.0, min(1.0, 0.55 + 0.45 * float(raw["source_quality_score"]))))
        except (TypeError, ValueError):
            pass
    return max(0.0, min(1.0, factor))

SYSTEM_PROMPT = """你是一个加密货币市场多源信号融合专家（Synthesis）。

你的任务是将来自多个分析维度（{dimension_list}）的信号进行加权融合，输出综合研判。

【第一步：判定市场状态】
先根据证据判定当前处于哪种市场状态，再据此选择权重档位（高/中/低）。{state_count}种状态与对应权重矩阵：

| 市场状态 | 技术面 | 链上 | 衍生品 | 舆情{macro_column_header}|
|---------|-------|------|-------|------{macro_column_sep}|
| 趋势市（各维度一致看多/空） | 高 | 中 | 中 | 低{macro_trend_weight} |
| 震荡市（无明确方向） | 低 | 高 | 中 | 中{macro_range_weight} |
| 高波动/事件驱动 | 中 | 中 | 高 | 高（反向）{macro_event_weight} |{geopolitical_row}
【第二步：加权融合】
综合分 = Σ(bias_score × weight × confidence)，据此输出方向倾向与置信度。

信号融合要求：
1. 多源共振才可信：单一信号不下结论，需要 ≥2 维度同向
2. 权重不是简单地平均，要理解不同市场状态下的信号有效性差异
3. 对各 Agent 的 confidence 做校准（某些 Agent 可能在当前状态下置信度虚高）
4. 找出最可靠的关键价位（多重汇聚 > 单一来源）{macro_rule}
{last_rule_number}. 高波动/事件驱动时，极端舆情作为反向指标（极度贪婪→顶部信号，极度恐惧→底部信号）

仓位建议规则（若启用）：
- 置信度 < 0.6 → 观望（不建议开仓）
- 0.6 ≤ 置信度 < 0.75 → 轻仓试探
- 置信度 ≥ 0.75 且 ≥3 维度共振 → 标准仓
- 止损距离 > 8% → 下调仓位
- 必须提供止盈目标和失效条件，形成完整的仓位生命周期管理

输出格式（严格 JSON）：
```json
{{
  "direction": "bullish",
  "confidence": 0.72,
  "key_findings": ["技术面多头排列 + 衍生品空头拥挤 = 看多共振"],
  "market_state": "趋势市",
  "weighted_scores": {{{weighted_scores_example}}},
  "key_levels": {{
    "supports": [{{"level": 60000, "confluence": 3, "reason": "前低+POC+EMA200汇聚"}}],
    "resistances": [{{"level": 65000, "confluence": 2, "reason": "前高+布林上轨"}}]
  }},
  "risk_assessment": "主要风险：成交量未放大，警惕假突破",
  "position_advice": {{
    "action": "轻仓试探",
    "reason": "置信度0.72，止损位59800（距现价3.2%）",
    "stop_loss": 59800,
    "take_profit": [{{"level": 65000, "pct": 30, "reason": "前高阻力位，部分止盈"}}],
    "exit_conditions": ["MACD死叉确认", "价格跌破EMA50", "关联市场恐慌事件"],
    "position_lifecycle": "当前处于多头初期，建议分3批入场：50%现价+25%回调到EMA200+25%突破确认后加仓"
  }},
  "analysis": "详细分析文本..."
}}
```

【重要原则】
- 所有价格数字必须来自证据中 algorithm 计算的结果，不得自行发明
- 必须标注"本分析仅供参考，不构成投资建议"
- black_swan_risks 为必填字段，至少列出以下四类常备尾部风险（即便当前无明确迹象也须注明监测边界）：
  交易所安全事件（如被盗/宕机）、监管政策突变、稳定币脱锚风险、大额代币解锁
- confidence 反映了多源交叉验证后的综合把握度
""".format(
    dimension_list="技术面、链上、衍生品、舆情、宏观/地缘" if _MACRO_ENABLED else "技术面、链上、衍生品、舆情",
    state_count="四" if _MACRO_ENABLED else "三",
    macro_column_header=" | 宏观/地缘" if _MACRO_ENABLED else "",
    macro_column_sep="----------" if _MACRO_ENABLED else "",
    macro_trend_weight=" | 低" if _MACRO_ENABLED else "",
    macro_range_weight=" | 低" if _MACRO_ENABLED else "",
    macro_event_weight=" | 高" if _MACRO_ENABLED else "",
    geopolitical_row=(
        "\n| 地缘危机（战争/黑天鹅） | 低 | 中 | 中 | 中 | 最高 |" if _MACRO_ENABLED else ""
    ),
    macro_rule=(
        "\n5. 地缘危机场景：宏观/地缘权重拉满，技术面在黑天鹅面前会失效，"
        "必须在 risk_assessment 明确提示\"事件驱动行情，技术信号参考价值下降\""
        if _MACRO_ENABLED else ""
    ),
    last_rule_number="6" if _MACRO_ENABLED else "5",
    weighted_scores_example=(
        '"technical": 0.35, "onchain": 0.2, "derivatives": 0.25, "sentiment": 0.1, "macro": 0.1'
        if _MACRO_ENABLED
        else '"technical": 0.35, "onchain": 0.25, "derivatives": 0.25, "sentiment": 0.15'
    ),
)


async def run_synthesis(state: AnalysisState) -> AnalysisState:
    """Synthesis 节点：融合全部证据池 → 输出综合研判。

    若状态中包含 critic feedback（即 Critic 触发的 revise），会在 prompt 中注入
    批评意见，要求 LLM 定向修正而非简单重跑。
    """
    evidence = state.get("evidence_pool", [])
    symbol = state.get("symbol", "")
    critic_feedback = state.get("critic_feedback", [])
    is_revise = bool(critic_feedback)

    if not evidence:
        state["synthesis_result"] = {
            "error": "无可用证据，无法进行综合研判",
            "direction": "neutral",
            "confidence": 0.0,
        }
        state["trace"] = list(state.get("trace", [])) + [{"node": "synthesis", "result": "no_evidence"}]
        return state

    evidence_str = json.dumps(_compact_evidence_for_prompt(evidence), ensure_ascii=False, indent=2)

    # Critic revise 模式：在 prompt 中注入批评意见，要求定向修正
    revise_hint = ""
    if is_revise:
        feedback_text = "\n".join(f"- {fb}" for fb in critic_feedback)
        revise_hint = f"""
⚠️ 这是对抗审查后的修正轮次。上一轮研判被 Critic 指出了以下问题，请逐条修正：
{feedback_text}

要求：
1. 必须逐条回应以上批评（同意则修正，不同意则给出反驳理由）
2. 不能只是重复上一次的结论
3. 若 confidence 过高，请根据 critic_feedback 合理下调
"""  # noqa: E501

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"交易对: {symbol}\n\n各维度证据:\n{evidence_str}\n{revise_hint}\n请输出 JSON 格式的综合研判。注意：仓位建议开关={'启用' if config.ENABLE_POSITION_ADVICE else '关闭'}。"},  # noqa: E501
    ]

    reasoning = ""
    try:
        response = await hy3_client.achat(
            messages=messages,
            # 不传 model：由 UnifiedClient 根据当前 provider 自动选择 slow 模型
            reasoning_effort="high",
            temperature=0.3,
        )
        choice = response.choices[0]
        content = choice.message.content or "{}"
        reasoning = getattr(choice, "reasoning_content", None) or getattr(choice.message, "reasoning_content", None) or ""
        result = parse_json(content, default={"error": "JSON 解析失败", "raw": content[:500]})
        if not result.get("direction"):
            logger.warning("Synthesis 结果缺 direction(finish=%s)，回退规则化融合", getattr(choice, "finish_reason", "?"))
            result = _fallback_synthesis(evidence, symbol)
    except Exception as e:
        logger.warning("Synthesis LLM 调用失败: %s", e)
        result = _fallback_synthesis(evidence, symbol)

    if not config.ENABLE_POSITION_ADVICE:
        result.pop("position_advice", None)

    # 方向和置信度由结构化证据确定性校准；LLM 负责市场状态、解释和风险描述。
    # Critic 的风险折扣最多应用一次，避免多轮审查重复扣减。
    try:
        weight_multipliers = state.get("_agent_weight_multipliers")
        performance_report = state.get("_agent_performance")
        if not weight_multipliers:
            weight_multipliers, performance_report = await get_dynamic_weight_multipliers(symbol or None)
            state["_agent_weight_multipliers"] = weight_multipliers
            state["_agent_performance"] = performance_report
        result = _calibrate_synthesis_result(
            result,
            evidence,
            weight_multipliers=weight_multipliers,
            performance_report=performance_report,
        )
        result = _apply_critic_confidence_penalty(result, state)
        result = _apply_confidence_guard(result, evidence)
    except Exception as e_guard:
        logger.warning("置信度守卫异常: %s，使用 fallback", e_guard)
        result = _fallback_synthesis(evidence, symbol)

    # 终极守卫：确保 direction 永不为空
    if not result.get("direction"):
        result = _fallback_synthesis(evidence, symbol)

    # 字段类型归一化：LLM 有时会把 black_swan_risks / key_findings 输出为对象数组
    # （如 [{"type": "...", "description": "..."}]）而非 prompt 要求的纯字符串数组。
    # 前端 TS 类型声明为 string[]，若原样透传对象会在渲染时抛
    # "Objects are not valid as a React child"，导致整个应用白屏/黑屏。
    # 在这里统一归一化为字符串，从源头堵住（Critic 合并阶段也有同样归一化作为双重保险）。
    result["black_swan_risks"] = _stringify_list(result.get("black_swan_risks"))
    result["key_findings"] = _stringify_list(result.get("key_findings"))

    result["disclaimer"] = "⚠️ 本分析仅供参考学习，不构成任何投资建议。加密货币市场风险极高，请自行判断。"
    state["synthesis_result"] = result

    trace = state.get("trace", [])
    trace.append({
        "node": "synthesis",
        "direction": result.get("direction"),
        "confidence": result.get("confidence"),
        "market_state": result.get("market_state"),
        "reasoning": reasoning,
    })
    state["trace"] = trace

    return state


def _stringify_list(items: object) -> list[str]:
    """把可能混杂对象的列表统一归一化为字符串列表。

    LLM 有时不严格遵循 prompt 中"字符串数组"的要求，返回
    [{"type": "...", "description": "..."}] 这类对象数组。直接把对象当
    React 子节点渲染会导致前端崩溃（无 ErrorBoundary 兜底时表现为白屏/黑屏）。
    """
    if not isinstance(items, list):
        return []
    result: list[str] = []
    for item in items:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            type_ = item.get("type") or item.get("name") or item.get("risk") or item.get("point")
            desc = item.get("description") or item.get("detail") or item.get("reason")
            if type_ and desc:
                result.append(f"{type_}：{desc}")
            elif type_:
                result.append(str(type_))
            elif desc:
                result.append(str(desc))
            else:
                result.append(json.dumps(item, ensure_ascii=False))
        elif item is not None:
            result.append(str(item))
    return result


def _compact_evidence_for_prompt(evidence: list[dict]) -> list[dict]:
    """Keep decision facts while excluding bulky traces and article replicas."""
    compact: list[dict] = []
    allowed = {
        "agent", "symbol", "bias", "score", "confidence", "data_quality",
        "data_source", "evidence", "caveats", "key_levels", "raw_metrics",
        "key_events", "government_events", "government_event_confirmation", "liquidation_clusters", "options_snapshot",
    }
    for item in evidence:
        if not isinstance(item, dict):
            continue
        view = {key: item[key] for key in allowed if key in item}
        graph = item.get("news_event_graph")
        if isinstance(graph, dict):
            view["news_event_graph"] = {
                "article_count": graph.get("article_count", 0),
                "event_count": graph.get("event_count", 0),
                "confirmed_event_count": graph.get("confirmed_event_count", 0),
                "nodes": [
                    {key: node.get(key) for key in (
                        "event_id", "title", "published_at", "age_hours", "topics",
                        "independent_source_count", "verified_independent_source_count",
                        "source_quality", "cross_source_confirmed", "impact_score",
                    )}
                    for node in graph.get("nodes", [])[:10]
                    if isinstance(node, dict)
                ],
            }
        compact.append(view)
    return compact


def _fallback_synthesis(evidence: list[dict], symbol: str) -> dict:
    """LLM 不可用时的备用融合（按简单加权，不替代精心设计的市场状态矩阵）。"""
    all_bias = [e.get("bias", "neutral") for e in evidence]
    all_scores = [e.get("score", 50) for e in evidence]
    all_conf = [e.get("confidence", 0.5) for e in evidence]

    bull_count = all_bias.count("bullish")
    bear_count = all_bias.count("bearish")
    # 加权平均（按 confidence 加权而非简单平均）
    total_conf = sum(all_conf) or 1.0
    weighted_score = sum(s * c for s, c in zip(all_scores, all_conf)) / total_conf

    direction = "bullish" if bull_count > bear_count else ("bearish" if bear_count > bull_count else "neutral")
    # 置信度：共识度越高越可信
    total = len(all_bias) or 1
    consensus = max(bull_count, bear_count) / total
    adjusted_conf = round(min(avg_conf := sum(all_conf) / total, consensus * 0.9), 2)

    result = {
        "direction": direction,
        "confidence": adjusted_conf,
        "key_findings": [f"基于 {len(evidence)} 个维度加权融合（LLM 不可用）"],
        "market_state": "未确定（LLM 不可用）",
        "weighted_scores": {},
        "key_levels": {},
        "risk_assessment": "LLM 不可用，风险无法详细评估。请勿依赖此自动回退结果做交易决策。",
        "analysis": f"自动融合（加权）：多头信号 {bull_count}，空头信号 {bear_count}，加权综合分 {weighted_score:.0f}，共识度 {consensus:.0%}",
    }
    return _apply_confidence_guard(_calibrate_synthesis_result(result, evidence), evidence)


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _market_weight_profile(market_state: object) -> dict[str, float]:
    state = str(market_state or "")
    if "地缘" in state or "战争" in state or "黑天鹅" in state:
        return _MARKET_WEIGHTS["geopolitical"]
    if "高波动" in state or "事件" in state:
        return _MARKET_WEIGHTS["event"]
    if "震荡" in state:
        return _MARKET_WEIGHTS["range"]
    if "趋势" in state:
        return _MARKET_WEIGHTS["trend"]
    return _MARKET_WEIGHTS["default"]


def _evidence_signal(item: dict) -> float:
    """Convert one Agent's bias/score into a signed [-1, 1] signal."""
    score = max(0.0, min(100.0, _safe_float(item.get("score"), 50.0)))
    strength = abs(score - 50.0) / 50.0
    bias = str(item.get("bias") or "neutral").lower()
    if bias == "bullish":
        return max(0.10, strength)
    if bias == "bearish":
        return -max(0.10, strength)
    # A neutral Agent may still expose a weak numerical lean, but it must not
    # carry the same directional weight as an explicit bullish/bearish signal.
    return ((score - 50.0) / 50.0) * 0.35


def _effective_profile(
    profile: dict[str, float],
    weight_multipliers: dict[str, float] | None = None,
) -> dict[str, float]:
    # 宏观维度禁用时剥离 macro 权重并归一化，让权重矩阵呈现为
    # "天然四维框架"（无缺口、无可被推断的缺失维度）
    base = {
        agent: weight
        for agent, weight in profile.items()
        if _MACRO_ENABLED or agent != "macro"
    }
    multipliers = weight_multipliers or {}
    adjusted = {
        agent: weight * max(0.70, min(1.30, _safe_float(multipliers.get(agent), 1.0)))
        for agent, weight in base.items()
    }
    total = sum(adjusted.values()) or 1.0
    return {agent: weight / total for agent, weight in adjusted.items()}


def _compute_signal_summary(
    evidence: list[dict],
    profile: dict[str, float],
    weight_multipliers: dict[str, float] | None = None,
) -> dict:
    profile = _effective_profile(profile, weight_multipliers)
    valid = [
        item for item in evidence
        if isinstance(item, dict)
        and not item.get("error")
        and str(item.get("agent") or "") in profile
        and _safe_float(item.get("confidence")) > 0
        and _data_quality_factor(item) > 0
    ]
    total_profile_weight = sum(profile.values()) or 1.0
    covered_weight = sum(
        profile[str(item["agent"])] * _data_quality_factor(item)
        for item in valid
    )
    reliability_mass = sum(
        profile[str(item["agent"])]
        * _data_quality_factor(item)
        * max(0.0, min(1.0, _safe_float(item.get("confidence"))))
        for item in valid
    )
    if reliability_mass <= 0:
        return {
            "direction": "neutral",
            "confidence": 0.0,
            "direction_score": 50.0,
            "weighted_scores": {},
            "contributions": [],
            "calibration": {
                "data_coverage": 0.0,
                "signal_agreement": 0.0,
                "average_source_confidence": 0.0,
                "net_signal": 0.0,
                "neutral_band": _NEUTRAL_SIGNAL_BAND,
            },
        }

    net_numerator = 0.0
    directional_mass = 0.0
    bullish_mass = 0.0
    bearish_mass = 0.0
    bullish_agents: set[str] = set()
    bearish_agents: set[str] = set()
    normalized_weights: dict[str, float] = {}
    contributions: list[dict] = []
    for item in valid:
        agent = str(item["agent"])
        source_confidence = max(0.0, min(1.0, _safe_float(item.get("confidence"))))
        influence = profile[agent] * _data_quality_factor(item) * source_confidence
        signal = _evidence_signal(item)
        contribution = influence * signal / reliability_mass
        net_numerator += influence * signal
        normalized_weights[agent] = round(influence / reliability_mass, 3)
        contributions.append({
            "agent": agent,
            "bias": str(item.get("bias") or "neutral"),
            "score": round(_safe_float(item.get("score"), 50.0), 1),
            "source_confidence": round(source_confidence, 3),
            "effective_weight": round(influence / reliability_mass, 3),
            "contribution_points": round(50.0 * contribution, 2),
        })
        if str(item.get("bias") or "neutral").lower() in {"bullish", "bearish"}:
            mass = influence * abs(signal)
            directional_mass += mass
            if signal > 0:
                bullish_mass += mass
                bullish_agents.add(agent)
            elif signal < 0:
                bearish_mass += mass
                bearish_agents.add(agent)

    net_signal = max(-1.0, min(1.0, net_numerator / reliability_mass))
    raw_direction = "neutral"
    if net_signal > _NEUTRAL_SIGNAL_BAND:
        raw_direction = "bullish"
    elif net_signal < -_NEUTRAL_SIGNAL_BAND:
        raw_direction = "bearish"

    directional_agent_count = len(bullish_agents | bearish_agents)
    aligned_agents = bullish_agents if raw_direction == "bullish" else bearish_agents if raw_direction == "bearish" else set()
    # Direction and execution are separate decisions.  Preserve a clear
    # one-source or mixed-source directional forecast so the system can be
    # evaluated later, while confidence/action guards prevent it from being
    # presented as a trade-ready consensus.
    directional_evidence_weak = bool(raw_direction != "neutral" and len(aligned_agents) < 2)
    direction = raw_direction
    if direction == "bullish":
        aligned_mass = bullish_mass
    elif direction == "bearish":
        aligned_mass = bearish_mass
    else:
        aligned_mass = min(bullish_mass, bearish_mass)

    mass_agreement = aligned_mass / directional_mass if directional_mass > 0 else 0.0
    agent_agreement = (
        len(aligned_agents) / directional_agent_count
        if directional_agent_count > 0 else 0.0
    )
    # Weighted mass alone can report high agreement when one high-weight Agent
    # dominates an opposing low-weight Agent. Blend it with independent-Agent
    # agreement so the displayed calibration remains honest.
    agreement = 0.5 * mass_agreement + 0.5 * agent_agreement
    coverage = min(1.0, covered_weight / total_profile_weight)
    average_source_confidence = min(1.0, reliability_mass / covered_weight) if covered_weight else 0.0
    signal_strength = min(1.0, abs(net_signal) / 0.35)
    if direction == "neutral":
        balance = 1.0 - min(1.0, abs(net_signal) / _NEUTRAL_SIGNAL_BAND)
        confidence_factor = 0.55 + 0.20 * balance
    else:
        confidence_factor = 0.45 + 0.35 * agreement + 0.20 * signal_strength
    calibrated_confidence = max(0.0, min(0.95, average_source_confidence * coverage * confidence_factor))
    if directional_evidence_weak:
        calibrated_confidence = min(calibrated_confidence, _SINGLE_SOURCE_CONFIDENCE_CAP)
    return {
        "direction": direction,
        "confidence": round(calibrated_confidence, 2),
        "direction_score": round(50.0 + 50.0 * net_signal, 1),
        "weighted_scores": normalized_weights,
        "contributions": sorted(contributions, key=lambda item: abs(item["contribution_points"]), reverse=True),
        "calibration": {
            "data_coverage": round(coverage, 3),
            "signal_agreement": round(agreement, 3),
            "mass_agreement": round(mass_agreement, 3),
            "directional_agent_count": directional_agent_count,
            "aligned_agent_count": len(aligned_agents),
            "bullish_agent_count": len(bullish_agents),
            "bearish_agent_count": len(bearish_agents),
            # Kept for API compatibility; direction is no longer erased by
            # this condition.  Consumers should use directional_evidence_weak
            # to distinguish a forecast from a multi-Agent consensus.
            "direction_blocked": False,
            "directional_evidence_weak": directional_evidence_weak,
            "direction_block_reason": (
                "少于两个独立 Agent 同向，保留方向预测但禁止高置信度执行"
                if directional_evidence_weak else ""
            ),
            "direction_confidence_cap": (
                _SINGLE_SOURCE_CONFIDENCE_CAP if directional_evidence_weak else None
            ),
            "average_source_confidence": round(average_source_confidence, 3),
            "net_signal": round(net_signal, 3),
            "neutral_band": _NEUTRAL_SIGNAL_BAND,
        },
    }


def _build_timeframe_outlook(
    evidence: list[dict],
    current_summary: dict,
    weight_multipliers: dict[str, float] | None = None,
) -> dict[str, dict]:
    outlook: dict[str, dict] = {}
    for horizon, profile in _TIMEFRAME_WEIGHTS.items():
        summary = current_summary if horizon == "24H" else _compute_signal_summary(
            evidence,
            profile,
            weight_multipliers,
        )
        effective_profile = _effective_profile(profile, weight_multipliers)
        context = _TIMEFRAME_CONTEXT[horizon]
        drivers = [
            {
                "agent": item["agent"],
                "label": _AGENT_LABELS.get(item["agent"], item["agent"]),
                "bias": item["bias"],
                "contribution_points": item["contribution_points"],
            }
            for item in summary.get("contributions", [])[:3]
        ]
        directional_drivers = [driver["label"] for driver in drivers if driver["bias"] in {"bullish", "bearish"}]
        driver_text = "、".join(directional_drivers) if directional_drivers else "暂无明确方向信号"
        outlook[horizon] = {
            "label": context["label"],
            "focus": context["focus"],
            "risk": context["risk"],
            "direction": summary["direction"],
            "confidence": summary["confidence"],
            "direction_score": summary["direction_score"],
            "data_coverage": summary["calibration"]["data_coverage"],
            "dominant_agents": [
                item["agent"] for item in summary.get("contributions", [])[:2]
            ],
            "thesis": f"{context['label']}优先观察{context['focus']}；当前主导证据为{driver_text}。",
            "drivers": drivers,
            "weight_profile": {
                agent: round(weight, 3)
                for agent, weight in effective_profile.items()
                if _MACRO_ENABLED or agent != "macro"
            },
        }
    return outlook


def _counterfactual_condition(agent: str, target_bias: str, result: dict) -> str:
    levels = result.get("key_levels") if isinstance(result.get("key_levels"), dict) else {}
    supports = levels.get("supports", []) if isinstance(levels, dict) else []
    resistances = levels.get("resistances", []) if isinstance(levels, dict) else []
    if agent == "technical":
        candidates = resistances if target_bias == "bullish" else supports
        if candidates and isinstance(candidates[0], dict) and candidates[0].get("level") is not None:
            verb = "站稳" if target_bias == "bullish" else "跌破"
            return f"价格{verb} {candidates[0]['level']} 并由技术指标确认"
        return "价格结构与动量指标同步反向确认"
    conditions = {
        "macro": "24小时宏观事件影响反转，并由至少2个独立来源交叉确认",
        "derivatives": "OI、资金费率与清算结构连续两个采样周期反向",
        "sentiment": "情绪指标跨越中性区并保持反向趋势",
        "onchain": "交易所净流量方向反转并连续两个区块窗口确认",
    }
    return conditions.get(agent, f"{agent} 维度出现经数据确认的反向信号")


def _build_counterfactuals(
    evidence: list[dict],
    result: dict,
    profile: dict[str, float],
    weight_multipliers: dict[str, float] | None = None,
) -> list[dict]:
    current_score = _safe_float(result.get("direction_score"), 50.0)
    scenarios: list[dict] = []
    for index, item in enumerate(evidence):
        if not isinstance(item, dict) or item.get("data_quality") != "real":
            continue
        bias = str(item.get("bias") or "neutral").lower()
        agent = str(item.get("agent") or "")
        if bias not in {"bullish", "bearish"} or agent not in profile:
            continue

        neutralized = [dict(entry) if isinstance(entry, dict) else entry for entry in evidence]
        neutralized[index]["bias"] = "neutral"
        neutralized[index]["score"] = 50
        neutral_summary = _compute_signal_summary(neutralized, profile, weight_multipliers)
        target_bias = "bearish" if bias == "bullish" else "bullish"
        chosen_summary = neutral_summary
        mode = "neutralized"
        if neutral_summary["direction"] == result.get("direction"):
            flipped = [dict(entry) if isinstance(entry, dict) else entry for entry in evidence]
            flipped[index]["bias"] = target_bias
            flipped[index]["score"] = round(100.0 - _safe_float(item.get("score"), 50.0), 1)
            chosen_summary = _compute_signal_summary(flipped, profile, weight_multipliers)
            mode = "flipped"
        scenarios.append({
            "agent": agent,
            "condition": _counterfactual_condition(agent, target_bias, result),
            "change": f"{bias} -> {'neutral' if mode == 'neutralized' else target_bias}",
            "result_direction": chosen_summary["direction"],
            "result_score": chosen_summary["direction_score"],
            "score_change": round(chosen_summary["direction_score"] - current_score, 1),
            "crosses_direction_boundary": chosen_summary["direction"] != result.get("direction"),
        })
    scenarios.sort(
        key=lambda item: (item["crosses_direction_boundary"], abs(item["score_change"])),
        reverse=True,
    )
    return scenarios[:3]


def _calibrate_synthesis_result(
    result: dict,
    evidence: list[dict],
    weight_multipliers: dict[str, float] | None = None,
    performance_report: dict | None = None,
) -> dict:
    """Derive direction/confidence from evidence instead of LLM self-rating.

    Direction expresses the best current estimate. Confidence expresses data
    coverage, source reliability, directional agreement, and signal strength.
    """
    profile = _market_weight_profile(result.get("market_state"))
    model_direction = str(result.get("direction") or "neutral")
    model_confidence = max(0.0, min(1.0, _safe_float(result.get("confidence"), 0.0)))
    summary = _compute_signal_summary(evidence, profile, weight_multipliers)
    calibration = dict(summary["calibration"])
    calibration.update({
        "method": "deterministic_evidence_v2",
        "critic_penalty": 0.0,
        "dynamic_weight_multipliers": {
            agent: round(_safe_float(value, 1.0), 3)
            for agent, value in (weight_multipliers or {}).items()
            if _MACRO_ENABLED or agent != "macro"
        },
    })
    # 宏观禁用时过滤校准报告中的 macro 条目：该结果会整包进入 Critic 提示词，
    # 保留 macro 条目会让 Critic 感知到"存在但零样本"的维度
    if performance_report and not _MACRO_ENABLED:
        filtered_report = dict(performance_report)
        agents_section = filtered_report.get("agents")
        if isinstance(agents_section, dict) and "macro" in agents_section:
            filtered_report["agents"] = {
                k: v for k, v in agents_section.items() if k != "macro"
            }
        performance_report = filtered_report
    result.update({
        "model_direction": model_direction,
        "model_confidence": model_confidence,
        "direction": summary["direction"],
        "confidence": summary["confidence"],
        "direction_score": summary["direction_score"],
        "weighted_scores": summary["weighted_scores"],
        "agent_contributions": summary["contributions"],
        "confidence_calibration": calibration,
        "agent_performance_calibration": performance_report or {},
        "primary_horizon": "24H",
    })
    result["timeframe_outlook"] = _build_timeframe_outlook(
        evidence,
        summary,
        weight_multipliers,
    )
    result["counterfactuals"] = _build_counterfactuals(
        evidence,
        result,
        profile,
        weight_multipliers,
    )
    macro_signal = next(
        (item for item in evidence if isinstance(item, dict) and item.get("agent") == "macro"),
        None,
    )
    onchain_signal = next(
        (item for item in evidence if isinstance(item, dict) and item.get("agent") == "onchain"),
        None,
    )
    if isinstance(macro_signal, dict) and macro_signal.get("news_event_graph"):
        result["news_event_graph"] = macro_signal["news_event_graph"]
    elif _MACRO_ENABLED:
        # 保持图谱入口稳定可见：没有宏观证据时也输出一个可审计的空图，
        # 前端据此明确展示“本窗口无经时效校验事件”，而不是静默隐藏能力。
        # （宏观维度被禁用时不输出该字段——对下游 Critic/前端完全无感知，
        #   避免"宏观 0 覆盖"类的降级批评）
        result["news_event_graph"] = {
            "article_count": 0,
            "event_count": 0,
            "confirmed_event_count": 0,
            "nodes": [],
            "edges": [],
        }

    graph_nodes = result.get("news_event_graph", {}).get("nodes", [])
    chain_deposit_count = 0
    chain_deposit_value_eth = 0.0
    chain_deposit_value_btc = 0.0
    if isinstance(onchain_signal, dict):
        raw_metrics = onchain_signal.get("raw_metrics", {})
        try:
            chain_deposit_count = int(raw_metrics.get("government_exchange_deposit_count", 0) or 0)
        except (TypeError, ValueError):
            chain_deposit_count = 0
        try:
            chain_deposit_value_eth = float(raw_metrics.get("government_exchange_deposit_value_eth", 0) or 0)
        except (TypeError, ValueError):
            chain_deposit_value_eth = 0.0
        try:
            chain_deposit_value_btc = float(raw_metrics.get("government_exchange_deposit_value_btc", 0) or 0)
        except (TypeError, ValueError):
            chain_deposit_value_btc = 0.0
    government_nodes = [
        node for node in graph_nodes
        if isinstance(node, dict)
        and ({"government_crypto_sale", "government_transfer"} & set(node.get("topics", [])))
    ]
    government_confirmations = [
        assess_government_event(node, chain_deposit_count=chain_deposit_count)
        for node in government_nodes
    ]
    result["government_event_confirmation"] = {
        "events": government_confirmations,
        "confirmed_sale_count": sum(item["status"] == "confirmed_sale" for item in government_confirmations),
        "probable_sale_count": sum(item["status"] == "probable_sale" for item in government_confirmations),
        "chain_deposit_count": chain_deposit_count,
        "chain_deposit_value_eth": round(chain_deposit_value_eth, 4),
        "chain_deposit_value_btc": round(chain_deposit_value_btc, 8),
        "method": "news_source_plus_configured_chain_deposit_v1",
    }

    findings = result.get("key_findings", [])
    if not isinstance(findings, list):
        findings = []
    findings = [
        item for item in findings
        if not (isinstance(item, str) and item.startswith("规则化校准："))
    ]
    calibration_finding = (
        f"规则化校准：净信号 {summary['direction_score']:.1f}/100，"
        f"加权一致度 {summary['calibration']['signal_agreement']:.0%}，"
        f"方向 Agent {summary['calibration'].get('aligned_agent_count', 0)} "
        f"/{summary['calibration'].get('directional_agent_count', 0)}，"
        f"数据覆盖 {summary['calibration']['data_coverage']:.0%}"
    )
    if summary["calibration"].get("directional_evidence_weak"):
        result.setdefault("caveats", []).append(
            "当前方向由少于两个独立 Agent 支持，保留为方向预测但置信度受限，交易动作仍需观望"
        )
    result["key_findings"] = [calibration_finding] + findings
    return result


def _apply_critic_confidence_penalty(result: dict, state: AnalysisState) -> dict:
    penalty = max(-0.10, min(0.0, _safe_float(state.get("_critic_confidence_penalty"))))
    if penalty >= 0:
        return result
    result["confidence"] = round(max(0.0, _safe_float(result.get("confidence")) + penalty), 2)
    calibration = result.setdefault("confidence_calibration", {})
    calibration["critic_penalty"] = round(penalty, 2)
    return result


def _apply_trade_action_guard(result: dict) -> dict:
    """Low confidence changes the action, never the analytical direction."""
    confidence = max(0.0, min(1.0, _safe_float(result.get("confidence"))))
    direction = str(result.get("direction") or "neutral")
    should_observe = direction == "neutral" or confidence < 0.60
    result["forecast_mode"] = "directional" if direction in {"bullish", "bearish"} else "neutral"
    result["execution_mode"] = "observe" if should_observe else "execute"
    if not should_observe or not config.ENABLE_POSITION_ADVICE:
        return result

    if direction == "neutral":
        reason = "净信号处于中性区间，暂不建立方向性仓位"
    else:
        reason = f"方向倾向为{direction}，但方向把握度仅 {confidence * 100:.0f}%，低于 60% 执行阈值"
    advice = result.get("position_advice")
    if not isinstance(advice, dict):
        advice = {}
        result["position_advice"] = advice
    advice["action"] = "观望（自动）"
    advice["reason"] = reason
    return result


def _apply_confidence_guard(result: dict, evidence: list[dict]) -> dict:
    """Keep forecasts visible; let confidence/action express uncertainty."""
    # 统计有效维度（data_quality=real 且无 error）
    real_dims = [
        e for e in evidence
        if e.get("data_quality") == "real" and not e.get("error") and e.get("confidence", 0) > 0
    ]
    effective_count = len(real_dims)
    usable_dims = [
        e for e in evidence
        if e.get("data_quality") in {"real", "partial"}
        and not e.get("error")
        and e.get("confidence", 0) > 0
    ]
    conf = max(0.0, min(1.0, _safe_float(result.get("confidence"))))
    direction = str(result.get("direction") or "neutral")

    # 完全没有可用维度时才中止方向判断。单一真实/部分数据源仍可
    # 形成“方向预测”，但会强制低置信度并进入 observe 影子追踪，
    # 这样系统既不会假装有共识，也不会用 neutral 隐藏可评估的信号。
    if not usable_dims:
        dim_names = [d.get("agent", "?") for d in evidence]
        degraded = [d.get("agent", "?") for d in evidence if d.get("data_quality") != "real"]
        return {
            "direction": "neutral",
            "confidence": 0.0,
            "key_findings": [
                f"数据不足，无法形成有效研判（有效维度 {effective_count}/{len(evidence)}）",
                f"可用维度: {', '.join(dim_names)}",
                f"数据异常维度: {', '.join(degraded)}" if degraded else "",
                "建议等待更多数据源恢复后重新分析",
            ],
            "market_state": result.get("market_state", "未知"),
            "weighted_scores": result.get("weighted_scores", {}),
            "key_levels": result.get("key_levels", {}),
            "risk_assessment": "数据覆盖率不足，风险无法系统评估",
            "black_swan_risks": ["交易所安全事件", "监管政策突变", "稳定币脱锚风险", "大额代币解锁"],
            "position_advice": {"action": "观望", "reason": "有效数据维度不足，不建议交易决策"},
            "forecast_mode": "neutral",
            "execution_mode": "observe",
            "analysis": f"本次分析仅 {effective_count}/{len(evidence)} 个维度返回真实数据。系统已自动中止研判，请修复数据源后重试。",
        }

    if effective_count < 2:
        limited_cap = _LIMITED_DATA_CONFIDENCE_CAP if effective_count else 0.25
        conf = min(conf, limited_cap)
        result["confidence"] = round(conf, 2)
        calibration = result.setdefault("confidence_calibration", {})
        calibration["real_dimension_count"] = effective_count
        calibration["usable_dimension_count"] = len(usable_dims)
        calibration["limited_data"] = True
        calibration["direction_confidence_cap"] = limited_cap
        findings = result.get("key_findings", []) or []
        warning = (
            f"⚠ 仅 {len(usable_dims)} 个维度可用，保留方向预测但置信度封顶 {limited_cap:.0%}；"
            "该结果只进入观望影子追踪，不作为执行信号"
        )
        findings = [item for item in findings if not (isinstance(item, str) and item.startswith("⚠ 仅 "))]
        result["key_findings"] = [warning] + findings

    # 低把握只约束交易动作，不覆盖由证据计算出的方向倾向。
    if conf < 0.60:
        findings = result.get("key_findings", []) or []
        prefix = f"⚠ 当前方向倾向为{direction}，但把握度仅 {conf*100:.0f}%；方向保留，交易动作降级为观望"
        findings = [item for item in findings if not (isinstance(item, str) and item.startswith("⚠ 当前方向倾向为"))]
        result["key_findings"] = [prefix] + findings

    result = _apply_trade_action_guard(result)

    # 兜底 black_swan_risks
    if not result.get("black_swan_risks"):
        result["black_swan_risks"] = ["交易所安全事件", "监管政策突变", "稳定币脱锚风险", "大额代币解锁"]

    return result
