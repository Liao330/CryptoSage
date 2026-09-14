"""
衍生品 Agent —— 资金费率、未平仓合约、爆仓数据（快思考 + Function Calling）。
"""

import logging

from backend.agents.state import AnalysisState
from backend.agents.fc_base import run_function_calling
from backend.config import config
from backend.data.derivatives_client import derivatives_client
from backend.tools.definitions import DERIVATIVES_TOOLS
from backend.utils.asof import parse_as_of_ms
from backend.utils.json_utils import parse_signal

logger = logging.getLogger(__name__)

# 期权数据源可用性开关：禁用时对 Agent 完全无感（提示词/工具表/覆盖度
# 计算中均不出现期权概念，不做"获取失败"降级标注）
_OPTIONS_ENABLED = config.ENABLE_OPTIONS_TOOL

SYSTEM_PROMPT = """你是一个加密货币衍生品市场分析专家。

你的任务是分析衍生品市场数据（资金费率、OI、爆仓{options_scope}），输出结构化的衍生品信号。

分析要点：
1. 资金费率：正费率 → 多头拥挤（偏空），负费率 → 空头拥挤（偏多）。极端费率是反向指标
2. 未平仓合约（OI）：OI 增长 + 价格上涨 = 趋势确认；OI 增长 + 价格不涨 = 分歧信号
3. 爆仓分布：多头爆仓密集区 = 下方的潜在磁吸价格；空头爆仓密集区 = 上方的潜在磁吸价格
4. 爆仓密集区可以作为关键价位的佐证{options_rules}

输出格式（严格 JSON）：
```json
{{
  "bias": "bearish",
  "score": 35,
  "confidence": 0.7,
  "evidence": ["资金费率 0.08%，多头拥挤", "OI 高位持平，上攻乏力"],
  "caveats": ["爆仓数据仅限 Binance"],
  "analysis": "详细分析...",
  "liquidation_clusters": [...]
}}
```
""".format(
    options_scope="、期权交割" if _OPTIONS_ENABLED else "",
    options_rules="""
5. 期权交割：关注最近到期时间、Call/Put OI、最大痛点、Call/Put 墙。
   - 临近交割且现价靠近最大痛点，做市商对冲可能造成价格钉住或均值回归；
   - Call/Put OI 只说明仓位规模，不等于买方方向，不能简单断言“占优方必然护盘”；
   - 期权墙与最大痛点和现货/永续信号同向时可作为弱确认，冲突时必须标注“可能收割大头仓位”，降低置信度。""" if _OPTIONS_ENABLED else "",
)


async def run_derivatives_agent(state: AnalysisState) -> AnalysisState:
    """衍生品 Agent：通过 Function Calling 自主调用费率/OI/爆仓工具 → 输出信号。"""
    symbol_raw = state.get("symbol", "BTC-USDT")
    # 转为 Binance 格式：BTC-USDT → BTCUSDT, ETH-USDC → ETHUSDC, BTC-BUSD → BTCBUSD
    bin_symbol = symbol_raw.replace("-", "")
    # 确认有常见合约后缀（USDT/USDC/BUSD）；缺失时默认 USDT
    _valid_suffixes = ("USDT", "USDC", "BUSD")
    if not any(bin_symbol.endswith(s) for s in _valid_suffixes):
        bin_symbol = bin_symbol + "USDT"

    user_prompt = (
        f"交易对: {bin_symbol}\n\n"
        "请依次调用可用工具获取衍生品数据（资金费率 get_funding_rate、未平仓合约 get_open_interest、"
        "爆仓数据 get_liquidations"
        + ("、Deribit 期权交割与最大痛点 get_options_snapshot" if _OPTIONS_ENABLED else "")
        + "），综合分析后输出 JSON 格式的衍生品分析信号。"
        "务必在输出中包含 liquidation_clusters 字段（可来自 get_liquidations 结果）。"
    )
    async_tool_map = {
        "get_funding_rate": derivatives_client.get_funding_rate,
        "get_open_interest": derivatives_client.get_open_interest,
        "get_liquidations": derivatives_client.get_liquidations,
    }
    if _OPTIONS_ENABLED:
        async_tool_map["get_options_snapshot"] = derivatives_client.get_options_snapshot

    # as-of 回测模式：客户端按 before_ts 历史取数；不可回溯源（爆仓/期权）自动降级
    as_of_ms = parse_as_of_ms(state.get("as_of"))
    if as_of_ms is not None:
        def _asof_wrap(fn):
            async def wrapper(*args, **kwargs):
                kwargs.setdefault("before_ts", as_of_ms)
                return await fn(*args, **kwargs)
            return wrapper

        async_tool_map = {name: _asof_wrap(fn) for name, fn in async_tool_map.items()}

    tools = list(DERIVATIVES_TOOLS)
    if not _OPTIONS_ENABLED:
        tools = [t for t in tools if t.get("function", {}).get("name") != "get_options_snapshot"]

    reasoning = ""
    tool_calls_log: list = []
    try:
        fc = await run_function_calling(SYSTEM_PROMPT, user_prompt, tools, async_tool_map)
        reasoning = fc.get("reasoning_content", "") or ""
        tool_calls_log = fc.get("tool_calls_log", [])
        signal = parse_signal(fc.get("final_message", "{}"), "derivatives", bin_symbol, defaults={"liquidation_clusters": []})
    except Exception as e:
        logger.warning("衍生品 Agent Function Calling 失败，回退预取: %s", e)
        funding = await derivatives_client.get_funding_rate(bin_symbol, before_ts=as_of_ms)
        oi = await derivatives_client.get_open_interest(bin_symbol, before_ts=as_of_ms)
        liquidations = await derivatives_client.get_liquidations(bin_symbol, before_ts=as_of_ms)
        signal = _fallback_derivatives_signal(bin_symbol, funding, oi, liquidations)
        tool_calls_log = [{"function": "get_liquidations", "result": liquidations}]

    # 期权链是确定性辅助证据：即使 LLM 没有主动调用工具，也必须采集，
    # 避免衍生品信号长期只有永续合约数据。（禁用时整段跳过，包括 Deribit
    # 超时等待，Agent 对期权数据无感知）
    options_snapshot: dict = {}
    if _OPTIONS_ENABLED:
        options_snapshot = await derivatives_client.get_options_snapshot(symbol_raw, before_ts=as_of_ms)
        _merge_options_signal(signal, options_snapshot)

    # 从工具调用结果中提取爆仓集群，供 key_levels 佐证使用
    if not signal.get("liquidation_clusters"):
        for entry in tool_calls_log:
            if entry.get("function") == "get_liquidations":
                res = entry.get("result", {})
                if isinstance(res, dict) and res.get("liquidation_clusters"):
                    signal["liquidation_clusters"] = res["liquidation_clusters"]
                    break

    signal["thinking"] = reasoning

    # 数据可信度标注与原始关键数值
    signal["data_source"] = "gate/bn/okx"
    if options_snapshot.get("data_quality") == "real":
        signal["data_source"] += "/deribit"
    signal["data_quality"] = "degraded"
    signal["raw_metrics"] = {}
    for entry in tool_calls_log:
        fn = entry.get("function", "")
        res = entry.get("result", {}) if isinstance(entry.get("result"), dict) else {}
        if fn == "get_funding_rate":
            if res.get("data_quality") == "real":
                signal["data_quality"] = "real"
            signal["raw_metrics"]["funding_rate_pct"] = res.get("funding_rate_pct")
        elif fn == "get_open_interest":
            signal["raw_metrics"]["oi_change_pct"] = res.get("oi_change_pct")
            if res.get("source") not in (None, "none") and not res.get("error"):
                signal["data_quality"] = "real"
        elif fn == "get_liquidations":
            if res.get("data_quality") == "real":
                signal["data_quality"] = "real"
            elif res.get("source") not in (None, "none") and not res.get("error"):
                signal["data_quality"] = "real"
            signal["raw_metrics"]["dominant_side"] = res.get("dominant_side", "")
        elif fn == "get_options_snapshot":
            signal["raw_metrics"]["options_positioning_mode"] = res.get("positioning_mode", "")

    # 确定性采集的结果优先写入 raw_metrics，便于综合层和前端溯源。
    for key in (
        "hours_to_expiry", "delivery_risk", "call_put_oi_ratio", "max_pain",
        "max_pain_distance_pct", "call_wall", "put_wall", "options_positioning_mode",
    ):
        if key in options_snapshot:
            signal["raw_metrics"][key] = options_snapshot[key]
    perpetual_available = any(
        signal["raw_metrics"].get(key) is not None
        for key in ("funding_rate_pct", "oi_change_pct", "dominant_side")
    )
    if _OPTIONS_ENABLED:
        options_available = options_snapshot.get("data_quality") == "real"
        if perpetual_available and not options_available:
            # 永续部分是真实数据，但期权交割是本维度的关键增强信号，
            # 不能因此把衍生品整体宣称为 full coverage。
            signal["data_quality"] = "partial"
        if perpetual_available or options_available:
            signal["raw_metrics"]["coverage_score"] = 1.0 if (perpetual_available and options_available) else 0.60
            signal["raw_metrics"]["coverage_components"] = {
                "funding_rate": "funding_rate_pct" in signal["raw_metrics"],
                "open_interest": "oi_change_pct" in signal["raw_metrics"],
                "liquidations": bool(signal.get("liquidation_clusters")),
                "options": options_available,
            }
        if options_snapshot.get("data_quality") == "real":
            # 资金费率/OI 某一源不可达时，不能把已成功获取的期权证据整个丢弃。
            # 标记为 real，同时在 caveats 中保留缺失的永续数据说明。
            if signal.get("data_quality") == "degraded":
                signal["data_quality"] = "partial"
                signal.setdefault("caveats", []).append("部分永续合约数据源不可用，本次衍生品信号由期权/可用数据支撑")
        if options_available and perpetual_available:
            signal["data_quality"] = "real"
        elif options_available or perpetual_available:
            signal["data_quality"] = "partial"
    else:
        # 期权源被禁用：覆盖度只统计启用的组件，永续齐备即视为完整覆盖，
        # 不因"缺期权"降级 data_quality（Agent 对期权数据无感知）
        if perpetual_available:
            signal["raw_metrics"]["coverage_score"] = 1.0
            signal["raw_metrics"]["coverage_components"] = {
                "funding_rate": "funding_rate_pct" in signal["raw_metrics"],
                "open_interest": "oi_change_pct" in signal["raw_metrics"],
                "liquidations": bool(signal.get("liquidation_clusters")),
            }

    if "evidence_pool" not in state:
        state["evidence_pool"] = []
    state["evidence_pool"].append(signal)

    trace = state.get("trace", [])
    trace.append({
        "node": "derivatives",
        "symbol": bin_symbol,
        "signal_bias": signal.get("bias"),
        "score": signal.get("score"),
        "reasoning": reasoning,
        "tool_calls": [t.get("function") for t in tool_calls_log]
        + (["get_options_snapshot"] if _OPTIONS_ENABLED else []),
    })
    state["trace"] = trace

    return state


def _fallback_derivatives_signal(
    symbol: str, funding: dict, oi: dict, liquidations: dict, options: dict | None = None
) -> dict:
    """LLM 不可用时的备用信号。"""
    bias = "neutral"; score = 50
    evidence_list = []

    # 资金费率判断
    rate_pct = funding.get("funding_rate_pct", 0)
    if rate_pct > 0.05:
        bias = "bearish"; score = 35
        evidence_list.append(f"资金费率 {rate_pct}%，多头拥挤（偏空）")
    elif rate_pct < -0.05:
        bias = "bullish"; score = 65
        evidence_list.append(f"资金费率 {rate_pct}%，空头拥挤（偏多）")

    # OI 判断
    oi_change = oi.get("oi_change_pct", 0)
    if abs(oi_change) > 5:
        evidence_list.append(f"OI 变化 {oi_change}%")

    result = {
        "agent": "derivatives",
        "symbol": symbol,
        "bias": bias,
        "score": score,
        "confidence": 0.55,
        "evidence": evidence_list,
        "caveats": ["自动生成（LLM 不可用）"],
        "liquidation_clusters": liquidations.get("liquidation_clusters", []),
        "data_quality": "real" if any(
            item.get("source") not in (None, "none") and not item.get("error")
            for item in (funding, oi)
        ) else "degraded",
    }
    if options:
        _merge_options_signal(result, options)
    return result


def _merge_options_signal(signal: dict, options: dict) -> None:
    """将期权信号以低权重并入衍生品 Agent，保留护盘/收割不确定性。"""
    signal["options_snapshot"] = options
    if options.get("data_quality") != "real":
        signal.setdefault("caveats", []).append(
            f"期权数据不可用：{options.get('error', 'Deribit 无有效期权链')}"
        )
        return

    if signal.get("data_quality") == "degraded":
        signal["data_quality"] = "real"
        signal.setdefault("caveats", []).append("永续合约部分数据不可用，保留 Deribit 期权辅助信号")

    option_bias = str(options.get("bias") or "neutral")
    option_score = float(options.get("score", 50) or 50)
    original_bias = str(signal.get("bias") or "neutral")
    original_score = float(signal.get("score", 50) or 50)
    if option_bias in {"bullish", "bearish"}:
        if original_bias == "neutral":
            # 只有期权方向时允许提供弱方向，置信度封顶，避免单一市场误导。
            signal["bias"] = option_bias
            signal["score"] = round(option_score)
            signal["confidence"] = min(float(signal.get("confidence", 0.4) or 0.4), 0.45)
        else:
            # 期权仅占衍生品 Agent 内部 25%，不覆盖资金费率/OI/爆仓判断。
            merged_score = original_score * 0.75 + option_score * 0.25
            signal["score"] = round(merged_score)
            if option_bias == original_bias:
                signal["confidence"] = min(0.75, float(signal.get("confidence", 0.4) or 0.4) + 0.04)
            else:
                signal.setdefault("caveats", []).append(
                    "期权定位与永续合约方向冲突，可能存在做市商护盘或收割大头仓位，已降低期权影响"
                )
                signal["confidence"] = min(float(signal.get("confidence", 0.4) or 0.4), 0.5)

    evidence = signal.setdefault("evidence", [])
    interpretation = options.get("interpretation")
    if interpretation and not any("期权" in str(item) for item in evidence):
        expiry_hours = options.get("hours_to_expiry")
        max_pain = options.get("max_pain")
        ratio = options.get("call_put_oi_ratio")
        evidence.append(
            f"期权交割 {expiry_hours}h，最大痛点 {max_pain or '未知'}，Call/Put OI {ratio or '未知'}：{interpretation}"
        )
    signal.setdefault("caveats", []).append(options.get("caveat", "期权 OI 不能直接区分买卖方"))
