"""
宏观/地缘 Agent —— 联网搜索宏观事件，评估对加密货币影响（快思考）。
"""

import logging

from backend.agents.state import AnalysisState
from backend.agents.fc_base import run_function_calling
from backend.data.macro_client import macro_client
from backend.data.news_graph import assess_government_event, build_news_event_graph
from backend.tools.definitions import MACRO_TOOLS
from backend.utils.asof import UNAVAILABLE_HINT, parse_as_of_ms
from backend.utils.json_utils import parse_signal

logger = logging.getLogger(__name__)

CURRENT_NEWS_WINDOW = "24h"
CURRENT_NEWS_MAX_AGE_HOURS = 24.0
MAX_NEWS_SEARCH_CALLS = 10
MAX_MODEL_NEWS_SEARCH_CALLS = 3
PRIORITY_FOCUS_QUERIES = (
    "bitcoin crypto market",
    "ethereum crypto market",
    "SEC crypto regulation",
    "stablecoin legislation",
    "FOMC interest rates",
)
MANDATORY_MARKET_QUERIES = (
    "government Bitcoin sale",
    "government Ethereum sale",
    "US Marshals Bitcoin",
)
US_POLICY_FOCUS_QUERIES = (
    "Federal Reserve inflation",
    "US Congress crypto",
    "US Treasury crypto",
)
DETERMINISTIC_PRIORITY_QUERIES = (
    *MANDATORY_MARKET_QUERIES,
    "bitcoin ethereum crypto",
    "SEC crypto regulation",
    "FOMC interest rates",
    "US Congress crypto",
)

# 中国国内新闻不作为本 Agent 的方向性证据。国际媒体报道美国政策时，
# 只有在文章本身以中国市场/政策为主题时才过滤。
CHINA_NEWS_TERMS = (
    "china", "chinese", "beijing", "shanghai", "shenzhen", "hong kong",
    "mainland", "pboc", "yuan", "renminbi", "中国", "中文", "北京", "上海",
    "深圳", "香港", "人民币", "央行数字货币", "中共",
)
CHINA_SOURCE_TERMS = (
    "新华社", "央视", "财联社", "澎湃", "证券时报", "第一财经", "中国新闻网",
    "环球时报", "新浪财经", "腾讯新闻", "网易财经", "东方财富", "华尔街见闻",
    "xinhuanet", "cgtn", "cctv", "yicai", "eastmoney", "sina.com.cn",
)
CRYPTO_FOCUS_TERMS = (
    "bitcoin", "btc", "ethereum", "eth", "crypto", "cryptocurrency", "digital asset",
    "stablecoin", "defi", "token", "blockchain", "coinbase", "binance", "kraken",
    "deribit", "sec", "cftc", "etf", "us marshals", "比特币", "以太坊",
)
US_POLICY_FOCUS_TERMS = (
    "federal reserve", "fed ", "fomc", "interest rate", "inflation", "cpi", "pce",
    "treasury", "congress", "senate", "house bill", "white house", "sec", "cftc",
    "us government", "u.s. government", "election", "tariff", "sanction", "fiscal",
    "monetary policy", "利率", "美联储", "美国政府",
)
GLOBAL_RISK_TERMS = ("iran", "israel", "opec", "oil", "war", "conflict", "sanction")

SYSTEM_PROMPT = """你是一个宏观经济学和地缘政治分析专家，专注于评估宏观事件对加密货币市场的影响。

你的任务：
1. 搜索并识别近期宏观/地缘政治事件
2. 评估这些事件对 BTC/ETH 的潜在影响方向和强度
3. 特别关注：战争/冲突 → 避险情绪；FOMC/利率 → 流动性影响；监管政策 → 合规风险

影响判断逻辑：
- 地缘冲突升级（如美伊）→ 短期避险利空 BTC（资金逃离风险资产），但长期可能利好（去中心化价值）
- FOMC 鹰派（加息）→ 利空（收紧流动性）
- FOMC 鸽派（降息）→ 利好（宽松流动性）
- 重大监管打击 → 利空
- 法币危机/高通胀 → 利好 BTC（避险叙事）

输出格式（严格 JSON）：
```json
{
  "bias": "bearish",
  "score": 35,
  "confidence": 0.5,
  "evidence": ["美伊冲突持续升级，油价飙升，风险资产承压"],
  "caveats": ["地缘影响短期 vs 长期方向可能不同"],
  "key_events": [{"event": "美伊冲突", "impact": "bearish", "severity": "high"}],
  "analysis": "详细分析..."
}
```
"""

async def run_macro_agent(state: AnalysisState) -> AnalysisState:
    """宏观 Agent：通过 Function Calling 自主决定搜索关键词并调用联网搜索工具 → 输出信号。"""
    symbol = state.get("symbol", "BTC-USDT")
    base = symbol.split("-")[0]
    query = state.get("query", "分析当前宏观环境")

    # as-of 回测模式：联网新闻检索为实时数据不可回溯，直接产出降级中性信号
    # （不检索、绝不用"当前"新闻冒充历史——那会把未来泄漏进分析）
    if parse_as_of_ms(state.get("as_of")) is not None:
        signal = {
            "agent": "macro",
            "symbol": base,
            "bias": "neutral",
            "score": 50,
            "confidence": 0.0,
            "evidence": [],
            "caveats": [UNAVAILABLE_HINT],
            "analysis": "",
            "data_source": "none",
            "data_quality": "degraded",
        }
        state.setdefault("evidence_pool", []).append(signal)
        state.setdefault("trace", []).append({
            "node": "macro", "symbol": base, "signal_bias": "neutral",
            "score": 50, "reasoning": "as-of 模式：新闻检索不可回溯，降级为中性",
            "tool_calls": [],
        })
        return state

    focus_queries = PRIORITY_FOCUS_QUERIES + MANDATORY_MARKET_QUERIES + US_POLICY_FOCUS_QUERIES
    user_prompt = (
        f"币种: {base}\n用户查询: {query}\n\n"
        "search_macro_news 已接入真实联网搜索，并会返回 freshness_verified、published_at、age_hours。\n"
        "本 Agent 的新闻范围只覆盖币圈动态、美国政治/经济/货币政策和直接影响加密资产的全球风险；"
        "不要检索中国国内政策、中文财经媒体或中国市场新闻。优先覆盖以下查询类别："
        f"{'、'.join(focus_queries)}。"
        "请自主、充分地检索多个关键词（FOMC 利率、加密监管、交易所安全事件、稳定币脱锚等对市场影响最大的类别），"
        f"并且必须额外检查以下政府资产事件查询：{', '.join(MANDATORY_MARKET_QUERIES)}；"
        "不能因为搜索结果为空就跳过这些查询，空结果也要记录到 caveats。"
        "确保覆盖近期最相关的宏观/地缘政治新闻后，"
        "基于**真实检索到的新闻**评估对加密货币市场的影响方向与强度，再输出 JSON 格式的宏观分析信号。\n"
        "分析质量优先于速度，不要因为担心耗时而过早停止检索或省略关键类别。\n"
        "⚠ 每次调用 search_macro_news 的 query 参数必须精简为 **2-3 个核心词**（如 '稳定币 脱锚'、"
        "'FOMC 利率'），**不要**额外拼接年份/月份文字（如 '2026'、'July'）——底层是全词精确匹配，"
        "词越多越难命中任何新闻，堆砌关键词会导致检索直接返回 0 结果。当前方向判断的有效窗口由代码层"
        "强制为 24h，即使请求 7d/30d 也不会让超过 24 小时的新闻进入方向性证据。\n"
        "要求：\n"
        "- 只有 freshness_verified=true 的结果可以支撑方向判断；无可验证发布时间的结果只能写入 caveats；\n"
        "- evidence 中每条须对应具体检索到的新闻，并注明来源与 published_at，不得凭空编造；\n"
        "- 若某次检索返回空结果或 error，必须在 caveats 中如实说明'该方向未检索到有效新闻'，并相应下调 confidence；\n"
        "- 必须留意尾部风险类事件（交易所被盗、监管突袭、稳定币脱锚），检索到则计入 key_events；"
        "中国相关结果不会作为本 Agent 的方向性证据。"
    )
    reasoning = ""
    tool_calls_log: list = []
    search_call_count = 0

    async def _search_with_budget(query: str, time_window: str = CURRENT_NEWS_WINDOW) -> dict:
        nonlocal search_call_count
        if search_call_count >= MAX_MODEL_NEWS_SEARCH_CALLS:
            return {
                "query": query,
                "results": [],
                "provider": "budget_exhausted",
                "error": f"模型定向新闻查询已达到预留上限 {MAX_MODEL_NEWS_SEARCH_CALLS}",
                "freshness_verified": False,
                "effective_time_window": CURRENT_NEWS_WINDOW,
            }
        search_call_count += 1
        return await _search_current_news(query, time_window)

    try:
        # 不再人为限制搜索轮数：分析质量优先于速度，允许模型充分检索多个关键词
        # 以获取更完整的宏观证据，即使耗时更长。
        fc = await run_function_calling(
            SYSTEM_PROMPT,
            user_prompt,
            MACRO_TOOLS,
            {"search_macro_news": _search_with_budget},
            max_rounds=6,
        )
        reasoning = fc.get("reasoning_content", "") or ""
        tool_calls_log = fc.get("tool_calls_log", [])
        signal = parse_signal(fc.get("final_message", "{}"), "macro", base, defaults={
            "confidence": 0.4,
            "caveats": ["宏观分析置信度天然较低，仅供参考"],
            "key_events": [],
        })
    except Exception as e:
        logger.warning("宏观 Agent Function Calling 失败，回退默认信号: %s", e)
        signal = _fallback_macro_signal(base)

    signal["thinking"] = reasoning

    # 系统级补搜核心币圈与美国政策类别，避免模型漏搜关键市场信息。
    model_search_entries = [
        entry for entry in tool_calls_log
        if entry.get("function") == "search_macro_news"
    ]
    searched_queries_set = {
        str(entry.get("result", {}).get("query"))
        for entry in model_search_entries
        if isinstance(entry.get("result"), dict)
        and entry.get("result", {}).get("query")
    }
    supplemental_search_count = 0
    # Do not duplicate a model-directed search session. The deterministic
    # suite is a fallback for LLM/tool failures or a model that never invoked
    # search_macro_news at all; normal sessions are audited by their actual
    # query list and the prompt's mandatory-query contract.
    model_queries_are_traceable = bool(model_search_entries) and all(
        isinstance(entry.get("result"), dict)
        and entry.get("result", {}).get("query")
        for entry in model_search_entries
    )
    fallback_priority_queries = (
        DETERMINISTIC_PRIORITY_QUERIES
        if not model_search_entries or model_queries_are_traceable
        else ()
    )
    for priority_query in fallback_priority_queries:
        if any(_query_overlap(priority_query, searched) for searched in searched_queries_set):
            continue
        if search_call_count >= MAX_NEWS_SEARCH_CALLS:
            break
        try:
            search_call_count += 1
            supplemental_search_count += 1
            supplemental_result = await _search_current_news(priority_query, CURRENT_NEWS_WINDOW)
            tool_calls_log.append({
                "function": "search_macro_news",
                "arguments": {"query": priority_query, "time_window": CURRENT_NEWS_WINDOW},
                "result": supplemental_result,
                "source": "deterministic_priority_query",
            })
            searched_queries_set.add(priority_query)
        except Exception as exc:
            logger.warning("核心新闻查询失败(%s): %s", priority_query, exc)

    # 聚合全部搜索轮次；只有逐条发布时间通过代码校验、且符合币圈/美国
    # 政策范围的结果才进入证据池。
    providers: set[str] = set()
    fresh_results: list[dict] = []
    total_result_count = 0
    unverified_result_count = 0
    filtered_china_result_count = 0
    filtered_offtopic_result_count = 0
    fetched_at_values: list[str] = []
    searched_queries: list[str] = []
    for entry in tool_calls_log:
        if entry.get("function") == "search_macro_news":
            res = entry.get("result", {})
            if isinstance(res, dict):
                if res.get("query"):
                    searched_queries.append(str(res["query"]))
                provider = res.get("provider")
                if provider:
                    providers.add(str(provider))
                results = res.get("results", [])
                total_result_count += len(results)
                verified = [result for result in results if _is_current_news_result(result)]
                for result in verified:
                    if _is_china_related(result):
                        filtered_china_result_count += 1
                    # Older/custom providers may omit the outer query field;
                    # preserve their timestamp-verified result for backwards
                    # compatibility. Native search providers always include
                    # query and therefore receive the strict topic filter.
                    elif res.get("query") and not _is_target_market_news(result):
                        filtered_offtopic_result_count += 1
                    else:
                        fresh_results.append(result)
                unverified_result_count += len(results) - len(verified)
                if res.get("fetched_at"):
                    fetched_at_values.append(str(res["fetched_at"]))

    # 把跨关键词、跨媒体转载聚为事件节点，避免转载数量放大宏观权重。
    event_graph = build_news_event_graph(fresh_results)
    # Top-N 只用于通用新闻展示；政府资产事件必须在完整图谱上核验，
    # 否则一条较旧但仍在 24H 窗口内的第二来源会被静默截掉。
    all_event_nodes = event_graph["nodes"]
    event_nodes = all_event_nodes[:15]
    government_nodes = [
        node for node in all_event_nodes
        if "government_crypto_sale" in node.get("topics", [])
        or "government_transfer" in node.get("topics", [])
    ]
    government_confirmations = [assess_government_event(node) for node in government_nodes]
    ages = [float(item["age_hours"]) for item in event_nodes if item.get("age_hours") is not None]
    signal["data_source"] = ",".join(sorted(providers)) if providers else "none"
    signal["data_quality"] = "real" if event_nodes else "degraded"
    source_quality_score = (
        sum(float(node.get("source_quality", 0.0) or 0.0) for node in event_nodes)
        / len(event_nodes)
        if event_nodes else 0.0
    )
    if event_nodes and source_quality_score < 0.65:
        signal["data_quality"] = "partial"
    signal["news_event_graph"] = event_graph
    signal["raw_metrics"] = {
        "event_count": len(event_nodes),
        "article_count": event_graph["article_count"],
        "confirmed_event_count": event_graph["confirmed_event_count"],
        "total_search_results": total_result_count,
        "unverified_result_count": unverified_result_count,
        "freshness_verified": bool(event_nodes),
        "effective_time_window": CURRENT_NEWS_WINDOW,
        "max_directional_news_age_hours": CURRENT_NEWS_MAX_AGE_HOURS,
        "freshest_age_hours": min(ages) if ages else None,
        "oldest_age_hours": max(ages) if ages else None,
        "fetched_at": max(fetched_at_values) if fetched_at_values else None,
        "government_event_count": len(government_nodes),
        "displayed_event_count": len(event_nodes),
        "source_quality_score": round(
            source_quality_score, 3
        ) if event_nodes else 0.0,
        "coverage_score": round(
            min(1.0, 0.55 + 0.45 * source_quality_score) if event_nodes else 0.0, 3
        ),
        "mandatory_news_queries": list(MANDATORY_MARKET_QUERIES),
        "searched_news_queries": searched_queries,
        "search_call_count": search_call_count,
        "search_call_budget": MAX_NEWS_SEARCH_CALLS,
        "model_search_call_count": len(model_search_entries),
        "supplemental_search_count": supplemental_search_count,
        "filtered_china_result_count": filtered_china_result_count,
        "filtered_offtopic_result_count": filtered_offtopic_result_count,
        "news_focus_profile": "crypto_market_and_us_policy",
        "priority_focus_queries": list(DETERMINISTIC_PRIORITY_QUERIES),
    }
    missing_mandatory_queries = [
        query for query in MANDATORY_MARKET_QUERIES
        if not any(_query_overlap(query, searched) for searched in searched_queries)
    ]
    signal["raw_metrics"]["missing_mandatory_news_queries"] = missing_mandatory_queries
    signal["raw_metrics"]["government_confirmation_statuses"] = [
        item["status"] for item in government_confirmations
    ]
    if missing_mandatory_queries:
        signal.setdefault("caveats", []).append(
            "本轮模型未明确执行政府资产查询：" + "、".join(missing_mandatory_queries)
        )
    if search_call_count >= MAX_NEWS_SEARCH_CALLS:
        signal.setdefault("caveats", []).append(
            f"新闻查询达到本轮上限 {MAX_NEWS_SEARCH_CALLS}，未继续扩展低优先级关键词"
        )
    if filtered_china_result_count:
        signal.setdefault("caveats", []).append(
            f"已过滤 {filtered_china_result_count} 条中国相关新闻，不纳入币圈方向判断"
        )
    if filtered_offtopic_result_count:
        signal.setdefault("caveats", []).append(
            f"已过滤 {filtered_offtopic_result_count} 条非币圈/美国政策目标新闻"
        )
    signal["fresh_news"] = [
        {
            "title": item.get("title", ""),
            "sources": item.get("sources", []),
            "independent_source_count": item.get("independent_source_count", 0),
            "verified_independent_source_count": item.get("verified_independent_source_count", item.get("independent_source_count", 0)),
            "cross_source_confirmed": item.get("cross_source_confirmed", False),
            "impact_score": item.get("impact_score", 0),
            "published_at": item.get("published_at", ""),
            "age_hours": item.get("age_hours"),
            "url": item.get("url", ""),
        }
        for item in event_nodes
    ]
    signal["evidence"] = [
        (
            f"{item.get('title', '')} | 独立来源 {item.get('independent_source_count', 0)} | "
            f"{item.get('published_at', '')} | 影响分 {item.get('impact_score', 0):.2f}"
        )
        for item in event_nodes[:5]
    ]
    if government_nodes:
        signal["government_events"] = [
            {
                "title": item.get("title", ""),
                "published_at": item.get("published_at", ""),
                "age_hours": item.get("age_hours"),
            "sources": item.get("sources", []),
            "cross_source_confirmed": item.get("cross_source_confirmed", False),
            "verified_independent_source_count": item.get("verified_independent_source_count", item.get("independent_source_count", 0)),
                "impact_score": item.get("impact_score", 0),
            }
            for item in government_nodes[:5]
        ]
        signal["government_event_confirmation"] = {
            "events": government_confirmations,
            "confirmed_sale_count": sum(item["status"] == "confirmed_sale" for item in government_confirmations),
            "probable_sale_count": sum(item["status"] == "probable_sale" for item in government_confirmations),
            "method": "news_source_plus_configured_chain_deposit_v1",
        }
        signal["evidence"].insert(
            0,
            f"检测到 {len(government_nodes)} 条美国政府/司法部 BTC/ETH 转移或出售相关事件，需核对链上地址与官方公告",
        )
        signal.setdefault("caveats", []).append(
            "政府钱包转移不等同于已出售；只有交易所入金、拍卖公告或多源确认才能提高出售判断置信度"
        )

    if event_nodes:
        signal["raw_metrics"]["key_events"] = "; ".join(
            str(item.get("title")) for item in event_nodes[:3]
        )
        signal["key_events"] = [
            {
                "event": item.get("title", ""),
                "published_at": item.get("published_at", ""),
                "independent_sources": item.get("independent_source_count", 0),
                "cross_source_confirmed": item.get("cross_source_confirmed", False),
                "impact_score": item.get("impact_score", 0),
            }
            for item in event_nodes[:5]
        ]
        if event_graph["confirmed_event_count"] == 0:
            signal["confidence"] = min(float(signal.get("confidence", 0.4)), 0.5)
            signal.setdefault("caveats", []).append("近期事件均缺少第二独立来源确认，宏观置信度已封顶为 50%")
    else:
        signal["bias"] = "neutral"
        signal["score"] = 50
        try:
            signal["confidence"] = min(float(signal.get("confidence", 0.3)), 0.3)
        except (TypeError, ValueError):
            signal["confidence"] = 0.3
        signal["key_events"] = []
        caveat = "未获得发布时间可验证且位于请求窗口内的新闻，宏观方向已强制设为中性"
        caveats = signal.setdefault("caveats", [])
        if caveat not in caveats:
            caveats.append(caveat)

    if "evidence_pool" not in state:
        state["evidence_pool"] = []
    state["evidence_pool"].append(signal)

    trace = state.get("trace", [])
    trace.append({
        "node": "macro",
        "symbol": base,
        "signal_bias": signal.get("bias"),
        "score": signal.get("score"),
        "reasoning": reasoning,
        "tool_calls": [t.get("function") for t in tool_calls_log],
    })
    state["trace"] = trace

    return state


async def _search_current_news(query: str, time_window: str = CURRENT_NEWS_WINDOW) -> dict:
    """Expose only a strict 24-hour search window to the current-market Agent."""
    result = await macro_client.search_macro_news(query, CURRENT_NEWS_WINDOW)
    normalized = dict(result) if isinstance(result, dict) else {"results": []}
    normalized["requested_time_window"] = time_window
    normalized["effective_time_window"] = CURRENT_NEWS_WINDOW
    return normalized


def _is_current_news_result(result: object) -> bool:
    """Defensively validate every directional news item at the Agent boundary."""
    if not isinstance(result, dict):
        return False
    if result.get("freshness_verified") is not True or not result.get("published_at"):
        return False
    try:
        age_hours = float(result.get("age_hours"))
    except (TypeError, ValueError):
        return False
    return 0.0 <= age_hours <= CURRENT_NEWS_MAX_AGE_HOURS


def _query_overlap(required: str, searched: str) -> bool:
    """宽松判断工具实际查询是否覆盖必查实体，兼容中英文空格/大小写。"""
    required_tokens = {token.casefold() for token in required.split() if token}
    searched_text = str(searched or "").casefold()
    return bool(required_tokens) and all(token in searched_text for token in required_tokens)


def _news_text(result: dict) -> str:
    return " ".join(
        str(result.get(field) or "")
        for field in ("title", "snippet", "source", "url")
    ).casefold()


def _is_china_related(result: dict) -> bool:
    """Return true for China-focused domestic reports excluded from this Agent."""
    text = _news_text(result)
    source = str(result.get("source") or "").casefold()
    if any(term in source for term in CHINA_SOURCE_TERMS):
        return True
    return any(term in text for term in CHINA_NEWS_TERMS)


def _is_target_market_news(result: dict) -> bool:
    """Keep crypto-market, US-policy, or directly relevant global-risk news."""
    text = _news_text(result)
    return (
        any(term in text for term in CRYPTO_FOCUS_TERMS)
        or any(term in text for term in US_POLICY_FOCUS_TERMS)
        or any(term in text for term in GLOBAL_RISK_TERMS)
    )


def _fallback_macro_signal(symbol: str) -> dict:
    return {
        "agent": "macro",
        "symbol": symbol,
        "bias": "neutral",
        "score": 50,
        "confidence": 0.3,
        "evidence": ["宏观搜索未找到显著事件（或未启用联网搜索）"],
        "caveats": ["宏观分析置信度较低，请以技术和链上数据为主"],
        "key_events": [],
        "data_source": "none",
        "data_quality": "degraded",
        "raw_metrics": {
            "event_count": 0,
            "freshness_verified": False,
            "effective_time_window": CURRENT_NEWS_WINDOW,
            "max_directional_news_age_hours": CURRENT_NEWS_MAX_AGE_HOURS,
        },
    }
