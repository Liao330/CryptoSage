"""Deterministic news event clustering and provenance graph."""

from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime, timezone
from urllib.parse import urlparse


_TOPIC_PATTERNS = {
    "federal_reserve": ("fomc", "fed ", "federal reserve", "美联储", "联储"),
    "interest_rate": ("interest rate", "rate hike", "rate cut", "利率", "加息", "降息"),
    "hawkish": ("hawkish", "鹰派"),
    "dovish": ("dovish", "鸽派"),
    "inflation": ("inflation", "cpi", "通胀"),
    "bitcoin": ("bitcoin", "btc", "比特币"),
    "ethereum": ("ethereum", "eth", "以太坊"),
    "crypto_regulation": ("sec", "regulation", "监管", "clarity act", "禁令"),
    "etf": ("etf", "交易所交易基金"),
    "stablecoin": ("stablecoin", "usdt", "usdc", "稳定币"),
    "depeg": ("depeg", "脱锚", "脱钩"),
    "exchange_security": ("hack", "hacker", "exploit", "被盗", "黑客", "漏洞", "安全事件"),
    "geopolitical_conflict": ("war", "strike", "conflict", "战争", "冲突", "袭击"),
    "iran": ("iran", "伊朗", "美伊"),
    "israel": ("israel", "以色列", "以伊"),
    "tariff": ("tariff", "trade war", "关税", "贸易战"),
    "oil": ("oil price", "crude oil", "油价", "原油"),
    "liquidation": ("liquidation", "爆仓", "清算"),
    "government_crypto_sale": (
        "government sells", "government sale", "government auction", "us government",
        "u.s. government", "us marshals", "u.s. marshals", "treasury sale",
        "政府出售", "政府拍卖", "美国政府", "美国司法部", "司法部出售",
    ),
    "government_transfer": (
        "government transfer", "government wallet", "government moves",
        "政府转移", "政府钱包", "政府地址",
    ),
}
_GENERIC_TOPICS = {"bitcoin", "ethereum"}
_NON_WORD = re.compile(r"[^a-z0-9\u4e00-\u9fff]+")
_EXCHANGE_TERMS = ("coinbase", "binance", "kraken", "交易所", "exchange wallet", "exchange address", "交易所地址")
_TRANSFER_TERMS = ("transfer", "transferred", "moves", "moved", "deposit", "deposited", "转入", "转移", "入金")
_AUCTION_TERMS = ("auction", "auctioned", "us marshals", "u.s. marshals", "拍卖", "竞拍")
_OFFICIAL_AUCTION_TERMS = ("us marshals", "u.s. marshals", "department of justice", "司法部", "官方拍卖")
_AGGREGATOR_SOURCE_TERMS = (
    "binance", "moomoo", "富途", "traders union", "coinmarketcap", "yahoo finance",
)


def _topics(title: str) -> set[str]:
    lowered = f" {title.casefold()} "
    return {
        topic for topic, patterns in _TOPIC_PATTERNS.items()
        if any(pattern in lowered for pattern in patterns)
    }


def _character_ngrams(title: str, size: int = 2) -> set[str]:
    text = _NON_WORD.sub("", title.casefold())
    if len(text) <= size:
        return {text} if text else set()
    return {text[index:index + size] for index in range(len(text) - size + 1)}


def _jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _event_similarity(left: dict, right: dict) -> float:
    government_score = _government_event_similarity(left, right)
    if government_score:
        return government_score
    left_topics = set(left.get("topics", []))
    right_topics = set(right.get("topics", []))
    meaningful_shared = (left_topics & right_topics) - _GENERIC_TOPICS
    topic_score = _jaccard(left_topics, right_topics)
    title_score = _jaccard(
        _character_ngrams(str(left.get("title") or "")),
        _character_ngrams(str(right.get("title") or "")),
    )
    if len(meaningful_shared) >= 2:
        return max(topic_score, 0.65 + 0.35 * title_score)
    if len(meaningful_shared) == 1 and topic_score >= 0.50:
        return max(topic_score, 0.45 + 0.35 * title_score)
    if left_topics - _GENERIC_TOPICS and right_topics - _GENERIC_TOPICS and not meaningful_shared:
        return min(title_score, 0.35)
    return title_score


def _source_name(item: dict) -> str:
    source = str(item.get("source") or "").strip()
    if source:
        return re.sub(r"[^a-z0-9\u4e00-\u9fff\uac00-\ud7af]+", " ", source.casefold()).strip()
    host = urlparse(str(item.get("url") or "")).hostname or ""
    return host.removeprefix("www.").casefold()


def _is_aggregator_source(source: str) -> bool:
    lowered = str(source or "").casefold()
    return any(term in lowered for term in _AGGREGATOR_SOURCE_TERMS)


def _government_event_similarity(left: dict, right: dict) -> float:
    """Match multilingual government transfer reports by asset/exchange/action."""
    left_text = f"{left.get('title', '')} {left.get('snippet', '')}".casefold()
    right_text = f"{right.get('title', '')} {right.get('snippet', '')}".casefold()
    government_terms = ("government", "u.s. government", "us government", "美国政府", "政府")
    asset_terms = (("bitcoin", "btc", "比特币"), ("ethereum", "eth", "以太坊"))
    government_hit = any(term in left_text for term in government_terms) and any(term in right_text for term in government_terms)
    exchange_hit = any(term in left_text for term in _EXCHANGE_TERMS) and any(term in right_text for term in _EXCHANGE_TERMS)
    transfer_hit = any(term in left_text for term in _TRANSFER_TERMS) and any(term in right_text for term in _TRANSFER_TERMS)
    shared_asset = any(
        any(term in left_text for term in terms) and any(term in right_text for term in terms)
        for terms in asset_terms
    )
    return 0.88 if government_hit and exchange_hit and transfer_hit and shared_asset else 0.0


def _event_id(topics: list[str], title: str) -> str:
    title_fingerprint = _NON_WORD.sub("", title.casefold())[:80]
    basis = "|".join(topics) + "::" + title_fingerprint
    return "evt_" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def build_news_event_graph(
    results: list[dict],
    now: datetime | None = None,
    cluster_threshold: float = 0.55,
) -> dict:
    """Cluster timestamp-verified articles and expose event provenance."""
    now = now or datetime.now(timezone.utc)
    prepared: list[dict] = []
    for item in results:
        if not isinstance(item, dict) or item.get("freshness_verified") is not True:
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        normalized = dict(item)
        normalized["topics"] = sorted(_topics(
            f"{title} {str(item.get('snippet') or '')}"
        ))
        prepared.append(normalized)
    prepared.sort(key=lambda item: str(item.get("published_at") or ""), reverse=True)

    clusters: list[list[dict]] = []
    for item in prepared:
        best_index = None
        best_score = 0.0
        for index, cluster in enumerate(clusters):
            score = _event_similarity(item, cluster[0])
            if score >= cluster_threshold and score > best_score:
                best_index = index
                best_score = score
        if best_index is None:
            clusters.append([item])
        else:
            clusters[best_index].append(item)

    nodes: list[dict] = []
    for articles in clusters:
        representative = articles[0]
        topics = sorted(set().union(*(set(article.get("topics", [])) for article in articles)))
        sources = sorted({source for article in articles if (source := _source_name(article))})
        verified_sources = [source for source in sources if not _is_aggregator_source(source)]
        age_hours = min(max(0.0, float(article.get("age_hours", 24.0))) for article in articles)
        freshness_decay = math.exp(-age_hours / 12.0)
        novelty_score = 1.0 / math.sqrt(len(articles))
        confirmation_score = min(1.0, 0.40 + 0.20 * len(sources))
        impact_score = freshness_decay * (0.55 + 0.45 * confirmation_score) * (0.75 + 0.25 * novelty_score)
        nodes.append({
            "event_id": _event_id(topics, str(representative.get("title") or "")),
            "title": representative.get("title", ""),
            "published_at": representative.get("published_at", ""),
            "age_hours": round(age_hours, 2),
            "topics": topics,
            "article_count": len(articles),
            "independent_source_count": len(sources),
            "verified_independent_source_count": len(verified_sources),
            "sources": sources,
            "source_quality": round(len(verified_sources) / max(1, len(sources)), 3),
            "aggregator_sources": [source for source in sources if _is_aggregator_source(source)],
            "cross_source_confirmed": len(verified_sources) >= 2,
            "novelty_score": round(novelty_score, 3),
            "freshness_decay": round(freshness_decay, 3),
            "impact_score": round(impact_score, 3),
            "url": representative.get("url", ""),
            "articles": [
                {
                    "title": article.get("title", ""),
                    "snippet": article.get("snippet", ""),
                    "source": article.get("source", ""),
                    "published_at": article.get("published_at", ""),
                    "url": article.get("url", ""),
                }
                for article in articles
            ],
        })
    nodes.sort(key=lambda node: (node["impact_score"], node["published_at"]), reverse=True)

    edges: list[dict] = []
    for left_index, left in enumerate(nodes):
        for right in nodes[left_index + 1:]:
            shared = sorted(set(left["topics"]) & set(right["topics"]))
            if shared:
                edges.append({
                    "source": left["event_id"],
                    "target": right["event_id"],
                    "type": "related_topic",
                    "topics": shared,
                })
    return {
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "article_count": len(prepared),
        "event_count": len(nodes),
        "confirmed_event_count": sum(1 for node in nodes if node["cross_source_confirmed"]),
        "nodes": nodes,
        "edges": edges,
    }


def assess_government_event(
    node: dict,
    chain_deposit_count: int = 0,
) -> dict:
    """融合新闻来源、交易所入金和拍卖公告，输出保守的政府资产事件状态。

    新闻本身只能产生线索：多源报道提高可信度；配置过的政府钱包真实入金才是
    链上确认；官方拍卖词只确认“拍卖公告”，不把公告误写成已经成交。
    """
    articles = node.get("articles", []) if isinstance(node, dict) else []
    text_parts = [str(node.get("title") or "")]
    text_parts.extend(str(article.get("title") or "") for article in articles if isinstance(article, dict))
    text_parts.extend(str(article.get("snippet") or "") for article in articles if isinstance(article, dict))
    text = " ".join(text_parts).casefold()
    topics = set(node.get("topics", [])) if isinstance(node, dict) else set()
    exchange_claim = (
        any(term in text for term in _EXCHANGE_TERMS)
        and any(term in text for term in _TRANSFER_TERMS)
    )
    auction_claim = any(term in text for term in _AUCTION_TERMS)
    official_auction = any(term in text for term in _OFFICIAL_AUCTION_TERMS)
    multi_source = bool(
        node.get("verified_independent_source_count", node.get("independent_source_count", 0)) >= 2
    )
    chain_confirmed = int(chain_deposit_count or 0) > 0

    confirmed_by: list[str] = []
    if chain_confirmed:
        confirmed_by.append("政府钱包→交易所链上入金")
    if multi_source:
        confirmed_by.append("第二独立新闻源")
    if official_auction:
        confirmed_by.append("官方/司法部拍卖公告")

    if chain_confirmed and exchange_claim:
        status = "confirmed_sale"
        label = "链上入金确认（仍需区分托管转移与实际成交）"
    elif exchange_claim and multi_source:
        status = "probable_sale"
        label = "多源确认的交易所转移/潜在出售"
    elif official_auction and auction_claim:
        status = "auction_announced"
        label = "已发现官方拍卖公告，尚未证明成交"
    elif exchange_claim:
        status = "unconfirmed_transfer"
        label = "单源交易所转移线索，未确认出售"
    else:
        status = "unconfirmed_event"
        label = "政府资产事件线索，未确认出售"

    missing: list[str] = []
    if not chain_confirmed:
        missing.append("政府钱包→交易所链上入金")
    if not multi_source:
        missing.append("第二独立新闻源")
    if not official_auction:
        missing.append("官方/司法部拍卖公告")
    return {
        "event_id": node.get("event_id", ""),
        "title": node.get("title", ""),
        "published_at": node.get("published_at", ""),
        "age_hours": node.get("age_hours"),
        "sources": list(node.get("sources", [])) if isinstance(node.get("sources"), list) else [],
        "independent_source_count": int(node.get("independent_source_count", 0) or 0),
        "verified_independent_source_count": int(
            node.get("verified_independent_source_count", node.get("independent_source_count", 0)) or 0
        ),
        "source_quality": node.get("source_quality", 1.0),
        "aggregator_sources": node.get("aggregator_sources", []),
        "status": status,
        "label": label,
        "exchange_transfer_claim": exchange_claim,
        "auction_claim": auction_claim,
        "official_auction": official_auction,
        "multi_source_confirmed": multi_source,
        "chain_confirmed": chain_confirmed,
        "confirmed_by": confirmed_by,
        "missing_confirmations": missing,
        "topics": sorted(topics & {"government_crypto_sale", "government_transfer", "bitcoin", "ethereum"}),
    }
