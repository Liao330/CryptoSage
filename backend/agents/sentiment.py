"""
舆情情绪 Agent —— 恐惧贪婪指数分析（快思考）。
"""

import logging

from backend.agents.state import AnalysisState
from backend.agents.fc_base import run_function_calling
from backend.data.sentiment_client import sentiment_client
from backend.tools.definitions import SENTIMENT_TOOLS
from backend.utils.asof import parse_as_of_ms
from backend.utils.json_utils import parse_signal

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一个加密货币市场情绪分析专家。

你的任务是分析市场情绪指标（恐惧贪婪指数），给出结构化的情绪信号。

分析要点：
1. 恐惧贪婪指数：0=极度恐惧, 100=极度贪婪
2. 反身性原则：极度恐惧(≤25)往往是底部区域（反向看多），极度贪婪(≥75)往往是顶部区域（反向看空）
3. 趋势变化：情绪从恐惧转向贪婪视为看涨信号，反之看跌
4. 情绪是反向指标，需要结合其他维度交叉验证

输出格式（严格 JSON）：
```json
{
  "bias": "bearish",
  "score": 30,
  "confidence": 0.6,
  "evidence": ["恐惧贪婪指数 78，极度贪婪，历史顶部分布"],
  "caveats": ["仅基于恐惧贪婪指数，未接入社交数据"],
  "analysis": "详细分析..."
}
```
"""


async def run_sentiment_agent(state: AnalysisState) -> AnalysisState:
    """舆情 Agent：通过 Function Calling 自主调用恐惧贪婪指数等工具 → 输出信号。"""
    symbol = state.get("symbol", "BTC-USDT")
    base = symbol.split("-")[0]
    # as-of 回测模式：情绪数据按天粒度回溯，社交实时数据自动降级（客户端内处理）
    as_of_ms = parse_as_of_ms(state.get("as_of"))

    user_prompt = (
        f"币种: {base}\n\n"
        "请调用可用工具获取市场情绪数据（恐惧贪婪指数 get_fear_greed、"
        "社交情绪 get_social_sentiment），基于反身性原则分析后输出 JSON 格式的情绪分析信号。"
    )

    async def _tool_get_fear_greed(limit: int = 30) -> dict:
        return await sentiment_client.get_fear_greed(limit=limit, before_ts=as_of_ms)

    async def _tool_get_social_sentiment(symbol_arg: str) -> dict:
        return await sentiment_client.get_social_sentiment(symbol_arg, before_ts=as_of_ms)

    async_tool_map = {
        "get_fear_greed": _tool_get_fear_greed,
        "get_social_sentiment": _tool_get_social_sentiment,
    }

    reasoning = ""
    tool_calls_log: list = []
    try:
        fc = await run_function_calling(SYSTEM_PROMPT, user_prompt, SENTIMENT_TOOLS, async_tool_map)
        reasoning = fc.get("reasoning_content", "") or ""
        tool_calls_log = fc.get("tool_calls_log", [])
        signal = parse_signal(fc.get("final_message", "{}"), "sentiment", base)
    except Exception as e:
        logger.warning("舆情 Agent Function Calling 失败，回退预取: %s", e)
        fg_data = await sentiment_client.get_fear_greed(limit=30, before_ts=as_of_ms)
        signal = _fallback_sentiment_signal(base, fg_data)

    signal["thinking"] = reasoning

    # 数据可信度标注
    signal["data_source"] = "alternative.me"
    signal["data_quality"] = "real"
    signal["raw_metrics"] = {}
    for entry in tool_calls_log:
        if entry.get("function") == "get_fear_greed":
            res = entry.get("result", {})
            if isinstance(res, dict):
                current = res.get("current", {})
                signal["raw_metrics"]["fgi_value"] = current.get("value") if isinstance(current, dict) else res.get("current", {}).get("value") if isinstance(res.get("current"), dict) else None
                break

    if "evidence_pool" not in state:
        state["evidence_pool"] = []
    state["evidence_pool"].append(signal)

    trace = state.get("trace", [])
    trace.append({
        "node": "sentiment",
        "symbol": base,
        "signal_bias": signal.get("bias"),
        "score": signal.get("score"),
        "reasoning": reasoning,
        "tool_calls": [t.get("function") for t in tool_calls_log],
    })
    state["trace"] = trace

    return state


def _fallback_sentiment_signal(symbol: str, fg_data: dict) -> dict:
    """LLM 不可用时的备用信号。"""
    current_val = fg_data.get("current", {}).get("value", 50)
    signal_str = fg_data.get("signal", "中性")

    # 反向指标
    if current_val >= 75:
        bias = "bearish"; score = 30
    elif current_val <= 25:
        bias = "bullish"; score = 70
    elif current_val > 55:
        bias = "bearish"; score = 40
    elif current_val < 45:
        bias = "bullish"; score = 60
    else:
        bias = "neutral"; score = 50

    return {
        "agent": "sentiment",
        "symbol": symbol,
        "bias": bias,
        "score": score,
        "confidence": 0.55,
        "evidence": [signal_str],
        "caveats": ["基于恐惧贪婪指数（未接入社交付费数据）"],
        "data_quality": "real",  # FGI 指数是真实数据
    }
