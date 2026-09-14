"""
链上 Agent —— 鲸鱼动向、交易所净流入/流出分析（快思考 + Function Calling）。
"""

import logging

from backend.agents.state import AnalysisState
from backend.agents.fc_base import run_function_calling
from backend.data.onchain_client import onchain_client
from backend.tools.definitions import ONCHAIN_TOOLS
from backend.utils.asof import UNAVAILABLE_HINT, parse_as_of_ms
from backend.utils.json_utils import parse_signal

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一个加密货币链上数据分析专家。

你的任务是分析链上数据（鲸鱼大额转账、交易所净流入/流出、地址覆盖和时间窗口），输出结构化的链上信号。

分析要点：
1. 鲸鱼（大额转账）动向：是转入交易所（潜在抛压）还是转出（潜在吸筹）？
2. 交易所净流量：净流入意味着市场参与者正将资产转移到交易所（倾向于卖出），净流出则相反
3. 注意因果推断陷阱："巨鲸转入交易所"未必是抛售，可能是做市、套利等
4. 只把 24 小时内、已标注交易所地址的转账用于方向判断；BTC mempool 大额输出没有交易所归属，只能作为活跃度/风险指标，不能当作净流入。
5. 同时报告交易笔数、流入/流出金额、最大单笔和地址覆盖率；单一大额转账不得单独改变方向。

输出格式（严格 JSON）：
```json
{
  "bias": "bullish",
  "score": 55,
  "confidence": 0.6,
  "evidence": ["交易所净流出 1200 ETH，大户提币"],
  "caveats": ["数据仅基于大额转账估算，非全量"],
  "analysis": "详细分析..."
}
```
"""


async def run_onchain_agent(state: AnalysisState) -> AnalysisState:
    """链上 Agent：通过 Function Calling 自主调用工具获取鲸鱼动向 + 交易所净流 → 输出信号。"""
    symbol = state.get("symbol", "ETH-USDT")
    base = symbol.split("-")[0]  # BTC or ETH

    user_prompt = (
        f"币种: {base}\n\n"
        "请依次调用可用工具获取链上数据（鲸鱼大额转账 get_whale_flows、"
        "交易所净流入/流出 get_exchange_netflow、政府钱包向交易所入金核验 get_government_exchange_deposits），并核对 24 小时窗口和地址覆盖，综合分析后输出 JSON 格式的链上分析信号。"
    )

    # as-of 回测模式：链上大额转账扫描为实时数据（mempool/最新区块），不可回溯。
    # 工具整体降级为"数据不可用"，绝不用当前链上数据冒充历史（杜绝未来泄漏）。
    as_of_ms = parse_as_of_ms(state.get("as_of"))

    async def _unavailable(*args, **kwargs) -> dict:
        return {"error": UNAVAILABLE_HINT, "data_quality": "degraded"}

    if as_of_ms is not None:
        async_tool_map = {
            "get_whale_flows": _unavailable,
            "get_exchange_netflow": _unavailable,
            "get_government_exchange_deposits": _unavailable,
        }
    else:
        async_tool_map = {
            "get_whale_flows": onchain_client.get_whale_flows,
            "get_exchange_netflow": onchain_client.get_exchange_netflow,
            "get_government_exchange_deposits": onchain_client.get_government_exchange_deposits,
        }

    reasoning = ""
    tool_calls_log: list = []
    try:
        fc = await run_function_calling(SYSTEM_PROMPT, user_prompt, ONCHAIN_TOOLS, async_tool_map)
        reasoning = fc.get("reasoning_content", "") or ""
        tool_calls_log = fc.get("tool_calls_log", [])
        signal = parse_signal(fc.get("final_message", "{}"), "onchain", base)
    except Exception as e:
        logger.warning("链上 Agent Function Calling 失败，回退预取: %s", e)
        netflow_data = await onchain_client.get_exchange_netflow(base)
        signal = _fallback_onchain_signal(base, netflow_data)
        government_data = await onchain_client.get_government_exchange_deposits(base)
        tool_calls_log = [
            {"function": "get_exchange_netflow", "result": netflow_data},
            {"function": "get_government_exchange_deposits", "result": government_data},
        ]

    signal["thinking"] = reasoning

    # 数据可信度标注（从 data client 返回中提取）
    signal["data_source"] = "etherscan" if base.upper() == "ETH" else "mempool.space"
    signal["data_quality"] = "degraded"
    signal["raw_metrics"] = {}
    for entry in tool_calls_log:
        fn = entry.get("function")
        res = entry.get("result", {})
        if not isinstance(res, dict):
            continue
        if fn == "get_exchange_netflow":
            if res.get("data_quality") == "real":
                signal["data_quality"] = "real"
            signal["raw_metrics"].update({
                "netflow_signal": res.get("signal", ""),
                "netflow_eth": res.get("netflow_eth"),
                "inflow_eth": res.get("inflow_eth"),
                "outflow_eth": res.get("outflow_eth"),
                "whale_activity": res.get("whale_activity"),
                "window_hours": res.get("window_hours", 24),
                "exchange_wallet_count": res.get("exchange_wallet_count", 0),
            })
        elif fn == "get_whale_flows":
            if res.get("data_quality") == "real":
                signal["data_quality"] = "real"
            txs = res.get("whale_transactions", [])
            values = [
                float(item.get("value_eth") or item.get("value_btc") or 0)
                for item in txs if isinstance(item, dict)
            ]
            signal["raw_metrics"].update({
                "whale_tx_count": res.get("total_whale_txs", len(txs)),
                "largest_whale_transfer": max(values) if values else 0,
                "exchange_wallet_count": res.get("exchange_wallet_count", signal["raw_metrics"].get("exchange_wallet_count", 0)),
                "window_hours": res.get("window_hours", signal["raw_metrics"].get("window_hours", 24)),
            })
        elif fn == "get_government_exchange_deposits":
            signal["raw_metrics"].update({
                "government_exchange_deposit_count": res.get("government_exchange_deposit_count", 0),
                "government_exchange_deposit_value_eth": res.get("government_exchange_deposit_value_eth", 0),
                "government_exchange_deposit_value_btc": res.get("government_exchange_deposit_value_btc", 0),
                "government_exchange_deposits": res.get("deposits", [])[:20] if isinstance(res.get("deposits"), list) else [],
                "government_chain_confirmed": bool(res.get("chain_confirmed")),
                "government_chain_reason": res.get("reason", ""),
            })
            if res.get("chain_confirmed"):
                signal.setdefault("evidence", []).append(
                    f"政府钱包向交易所入金已由链上确认：{res.get('government_exchange_deposit_count', 0)} 笔"
                )
            elif res.get("reason"):
                signal.setdefault("caveats", []).append(str(res["reason"]))

    if base.upper() == "BTC":
        signal.setdefault("caveats", []).append(
            "BTC 当前链上源为 mempool.space 大额输出活跃度，缺少交易所地址归属，不能等同精确净流入"
        )
        # mempool.space 的大额输出是真实链上数据，但不是交易所净流，
        # 因此不能把整个链上维度标成 full/real coverage。
        if signal.get("data_quality") == "real":
            signal["data_quality"] = "partial"
        signal.setdefault("raw_metrics", {})["coverage_score"] = 0.35
        signal["raw_metrics"]["coverage_components"] = {
            "whale_activity": True,
            "exchange_netflow": False,
            "government_wallet_binding": bool(signal["raw_metrics"].get("government_chain_confirmed")),
        }
    elif signal.get("data_quality") == "real":
        # ETH 的交易所净流和鲸鱼转账可用时为完整核心数据；政府地址核验
        # 是事件增强项，不把普通 ETH 市场分析误判为不可用。
        signal.setdefault("raw_metrics", {})["coverage_score"] = 0.80
        signal["raw_metrics"]["coverage_components"] = {
            "whale_activity": True,
            "exchange_netflow": bool(signal["raw_metrics"].get("netflow_eth") is not None),
            "government_wallet_binding": bool(signal["raw_metrics"].get("government_chain_confirmed")),
        }

    if "evidence_pool" not in state:
        state["evidence_pool"] = []
    state["evidence_pool"].append(signal)

    trace = state.get("trace", [])
    trace.append({
        "node": "onchain",
        "symbol": base,
        "signal_bias": signal.get("bias"),
        "score": signal.get("score"),
        "reasoning": reasoning,
        "tool_calls": [t.get("function") for t in tool_calls_log],
    })
    state["trace"] = trace

    return state


def _fallback_onchain_signal(symbol: str, netflow: dict) -> dict:
    """LLM 不可用时的备用信号。"""
    bias = "neutral"; score = 50
    signal_str = netflow.get("signal", "")
    if "偏多" in signal_str:
        bias = "bullish"; score = 60
    elif "偏空" in signal_str:
        bias = "bearish"; score = 40

    return {
        "agent": "onchain",
        "symbol": symbol,
        "bias": bias,
        "score": score,
        "confidence": 0.5,
        "evidence": [netflow.get("signal", ""), netflow.get("data_quality", "")],
        "caveats": ["链上数据基于大额转账估算"],
        "data_quality": netflow.get("data_quality", "degraded"),
        "raw_metrics": {
            "netflow_signal": netflow.get("signal", ""),
            "netflow_eth": netflow.get("netflow_eth"),
            "inflow_eth": netflow.get("inflow_eth"),
            "outflow_eth": netflow.get("outflow_eth"),
            "whale_activity": netflow.get("whale_activity"),
            "window_hours": netflow.get("window_hours", 24),
            "exchange_wallet_count": netflow.get("exchange_wallet_count", 0),
        },
    }
