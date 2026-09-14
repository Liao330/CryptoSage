"""
技术面 Agent —— K线、技术指标、关键价位分析（快思考 + Function Calling）。
"""

import logging

from backend.agents.state import AnalysisState
from backend.agents.fc_base import run_function_calling
from backend.tools.definitions import TECHNICAL_TOOLS
from backend.data.kline_repository import kline_repo
from backend.indicators.ta import calc_all_indicators
from backend.indicators.key_levels import calc_key_levels
from backend.utils.asof import parse_as_of_ms
from backend.utils.json_utils import parse_signal

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一个加密货币技术面分析专家。

你的任务是分析 K线数据和技术指标，给出结构化的市场信号。

工作方式（Function Calling）：
你可以自主调用以下工具获取数据，请先取数再下结论：
- get_klines：获取 K线行情摘要（最新价、近期高低、近期收盘序列）
- calc_indicators：计算 MA/MACD/RSI/BOLL 等技术指标
- calc_key_levels：从算法推导可追溯的关键支撑/阻力位
建议顺序：先 calc_indicators 看指标，再 calc_key_levels 拿价位，必要时用 get_klines 校验行情。

分析要点：
1. 趋势判断（多/空/震荡），基于 EMA 排列和关键价位
2. MACD 金叉/死叉信号
3. RSI 超买/超卖状态
4. 布林带位置（突破/通道内）
5. 关键支撑/阻力位（来自算法推导，非你臆造）
6. 成交量配合情况

输出格式（严格 JSON）：
```json
{
  "bias": "bullish",
  "score": 68,
  "confidence": 0.75,
  "key_levels": {"support": [60000, 59500], "resistance": [65000, 66500]},
  "evidence": ["4H MACD 金叉", "价格站上 EMA200"],
  "caveats": ["成交量未同步放大，警惕假突破"],
  "analysis": "详细分析文本..."
}
```

重要原则：
- 价位数字必须来自 calc_key_levels 的算法结果，你负责解释理由，不负责"发明"价格
- confidence 表示你对这组信号的把握程度（0=毫无把握, 1=绝对确定）
- score 表示该维度的看多强度（0=极端看空, 50=中性, 100=极端看多）
"""


async def run_technical_agent(state: AnalysisState) -> AnalysisState:
    """技术面 Agent：通过 Function Calling 自主取数（K线/指标/关键价位）→ 输出信号。

    关键价位始终以算法结果覆盖 LLM 输出，保证价位可追溯、非模型臆造。
    """
    symbol = state.get("symbol", "BTC-USDT")
    bar = "4H"
    # as-of 回测模式：所有取数只允许 ts < as_of（杜绝未来泄漏）
    as_of_ms = parse_as_of_ms(state.get("as_of"))

    def _with_provenance(payload: dict, rows: list[dict]) -> dict:
        sources = sorted({row.get("data_source", "unknown") for row in rows})
        qualities = {row.get("data_quality", "degraded") for row in rows}
        payload["data_source"] = ",".join(sources)
        payload["data_quality"] = "real" if qualities == {"real"} else "degraded"
        return payload

    # ── FC 工具实现（async；在 fc_base 子线程内经 asyncio.run 调用，故依赖无状态的 kline_repo）──
    async def _tool_get_klines(symbol: str, bar: str = "4H", limit: int = 200) -> dict:
        rows = await kline_repo.get_klines(symbol, bar, min(limit, 200), before_ts=as_of_ms)
        if not rows:
            return {"error": "K线数据获取失败"}
        recent = rows[-30:]
        return _with_provenance({
            "symbol": symbol,
            "bar": bar,
            "count": len(rows),
            "latest_close": rows[-1]["close"],
            "recent_high": max(k["high"] for k in recent),
            "recent_low": min(k["low"] for k in recent),
            "last_10_closes": [k["close"] for k in rows[-10:]],
        }, rows)

    async def _tool_calc_indicators(symbol: str, bar: str = "4H") -> dict:
        rows = await kline_repo.get_klines(symbol, bar, 200, before_ts=as_of_ms)
        if not rows:
            return {"error": "K线数据获取失败"}
        return _with_provenance(calc_all_indicators(rows), rows)

    async def _tool_calc_key_levels(symbol: str, bar: str = "4H") -> dict:
        rows = await kline_repo.get_klines(symbol, bar, 200, before_ts=as_of_ms)
        if not rows:
            return {"error": "K线数据获取失败"}
        return _with_provenance(calc_key_levels(rows, bar=bar), rows)

    async_tool_map = {
        "get_klines": _tool_get_klines,
        "calc_indicators": _tool_calc_indicators,
        "calc_key_levels": _tool_calc_key_levels,
    }

    user_prompt = (
        f"交易对: {symbol}\n周期: {bar}\n\n"
        "请依次调用工具获取技术指标（calc_indicators）与关键价位（calc_key_levels），"
        "必要时用 get_klines 校验行情，综合后输出 JSON 格式的技术面分析信号。"
    )

    reasoning = ""
    tool_calls_log: list = []
    algo_key_levels: dict | None = None
    fallback_rows: list[dict] = []

    try:
        fc = await run_function_calling(SYSTEM_PROMPT, user_prompt, TECHNICAL_TOOLS, async_tool_map)
        reasoning = fc.get("reasoning_content", "") or ""
        tool_calls_log = fc.get("tool_calls_log", [])
        signal = parse_signal(fc.get("final_message", "{}"), "technical", symbol)
        # 从工具结果里提取算法关键价位（用于确定性覆盖）
        for entry in tool_calls_log:
            if entry.get("function") == "calc_key_levels" and isinstance(entry.get("result"), dict):
                res = entry["result"]
                if "error" not in res:
                    algo_key_levels = res
                    break
    except Exception as e:
        logger.warning("技术面 Agent Function Calling 失败，回退预取: %s", e)
        rows = await kline_repo.get_klines(symbol, bar, 200, before_ts=as_of_ms)
        if not rows:
            return _set_error_signal(state, "technical", "K线数据获取失败")
        fallback_rows = rows
        indicators = calc_all_indicators(rows)
        algo_key_levels = calc_key_levels(rows, bar=bar)
        _with_provenance(algo_key_levels, rows)
        signal = _fallback_technical_signal(symbol, indicators, algo_key_levels)

    # 若 LLM 未调用 calc_key_levels（或结果异常），兜底重算，确保价位始终来自算法
    if algo_key_levels is None:
        try:
            rows = await kline_repo.get_klines(symbol, bar, 200, before_ts=as_of_ms)
            algo_key_levels = calc_key_levels(rows, bar=bar) if rows else {}
            if rows:
                fallback_rows = rows
                _with_provenance(algo_key_levels, rows)
        except Exception as e:
            logger.warning("关键价位兜底计算失败: %s", e)
            algo_key_levels = {}

    # 用算法价位覆盖信号，保证可追溯
    signal["key_levels"] = {
        "supports": [
            {"level": s["level"], "confluence": s.get("confluence_count", 1)}
            for s in algo_key_levels.get("support_levels", [])[:3]
        ],
        "resistances": [
            {"level": r["level"], "confluence": r.get("confluence_count", 1)}
            for r in algo_key_levels.get("resistance_levels", [])[:3]
        ],
    }
    signal["thinking"] = reasoning

    # 注入数据可信度标注与原始关键数值（供 Orchestrator 决策 + 前端溯源）
    provenance = [
        entry.get("result", {})
        for entry in tool_calls_log
        if isinstance(entry.get("result"), dict)
    ]
    if algo_key_levels:
        provenance.append(algo_key_levels)
    if fallback_rows:
        provenance.append(_with_provenance({}, fallback_rows))

    sources = sorted({
        item.get("data_source") for item in provenance if item.get("data_source")
    })
    qualities = {
        item.get("data_quality", "degraded") for item in provenance
    }
    signal["data_source"] = ",".join(sources) if sources else "unknown"
    signal["data_quality"] = "real" if qualities == {"real"} else "degraded"
    signal["raw_metrics"] = {
        "rsi": None,
        "close": algo_key_levels.get("current_price"),
    }
    # 从工具调用日志里提取真实数值
    for entry in tool_calls_log:
        if entry.get("function") == "calc_indicators":
            res = entry.get("result", {})
            if isinstance(res, dict):
                rsi_data = res.get("rsi", {})
                macd_data = res.get("macd", {})
                signal["raw_metrics"]["rsi"] = rsi_data.get("value") if isinstance(rsi_data, dict) else None
                signal["raw_metrics"]["macd_signal"] = macd_data.get("crossover") or macd_data.get("trend") if isinstance(macd_data, dict) else None
                signal["raw_metrics"]["close"] = res.get("latest_close")
                break

    if signal["data_quality"] != "real":
        try:
            signal["confidence"] = min(float(signal.get("confidence", 0.5)), 0.45)
        except (TypeError, ValueError):
            signal["confidence"] = 0.45
        caveats = signal.setdefault("caveats", [])
        caveat = "K线来自合成数据或本地缓存，已降低技术面置信度"
        if caveat not in caveats:
            caveats.append(caveat)

    # 添加到证据池
    if "evidence_pool" not in state:
        state["evidence_pool"] = []
    state["evidence_pool"].append(signal)

    # 留痕（对齐其余数据 Agent：附思维链与工具编排轨迹）
    trace = state.get("trace", [])
    trace.append({
        "node": "technical",
        "symbol": symbol,
        "signal_bias": signal.get("bias"),
        "score": signal.get("score"),
        "reasoning": reasoning,
        "tool_calls": [t.get("function") for t in tool_calls_log],
    })
    state["trace"] = trace

    return state


def _fallback_technical_signal(symbol: str, indicators: dict, key_levels: dict) -> dict:
    """备用：当 LLM 不可用时，用指标结果构造信号。"""
    signals = indicators.get("signals", [])
    bull = sum(1 for s in signals if s.get("signal") == "bullish")
    bear = sum(1 for s in signals if s.get("signal") == "bearish")
    total = len(signals) or 1
    score = int(50 + (bull - bear) * 10)
    score = max(0, min(100, score))

    bias = "bullish" if score >= 60 else ("bearish" if score <= 40 else "neutral")

    return {
        "agent": "technical",
        "symbol": symbol,
        "bias": bias,
        "score": score,
        "confidence": 0.6,
        "key_levels": key_levels,
        "evidence": [s["detail"] for s in signals],
        "caveats": ["自动生成（LLM 不可用）"],
        "analysis": indicators.get("trend_summary", ""),
        "data_quality": key_levels.get("data_quality", "degraded"),
    }


def _set_error_signal(state: AnalysisState, agent: str, error: str) -> AnalysisState:
    """在证据池中添加错误信号。"""
    signal = {
        "agent": agent,
        "symbol": state.get("symbol", ""),
        "bias": "neutral",
        "score": 50,
        "confidence": 0.0,
        "key_levels": {},
        "evidence": [],
        "caveats": [error],
    }
    if "evidence_pool" not in state:
        state["evidence_pool"] = []
    state["evidence_pool"].append(signal)
    trace = state.get("trace", [])
    trace.append({"node": agent, "error": error})
    state["trace"] = trace
    return state
