"""
宏观/地缘数据 Client —— 自建联网搜索 tool（免费、零成本、无需申请）。

设计原则（遵循"零假数据、真联网"）：
- 不再使用 mock / 占位分支：search_macro_news 每次都真正发起 HTTP 搜索请求。
- 默认走免费公开搜索源，无需 API Key：
    1. Google News RSS (news.google.com/rss/search) —— 主源，新闻质量高，天然带发布时间/来源。
    2. DuckDuckGo Lite (lite.duckduckgo.com) —— 备源，通用网页搜索。
    3. tavily / serpapi —— 可选（配 Key 时启用），作为增强源。
- 该 tool 以标准 Function Calling 形式暴露给 Hy3（Chat Completions 协议），
  由模型自主决定搜索关键词并消费结构化结果，成本为零。
"""

import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import quote_plus

from backend.config import config
from backend.data.ssl_utils import make_client

logger = logging.getLogger(__name__)

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
DUCKDUCKGO_LITE = "https://lite.duckduckgo.com/lite/"

_TIME_WINDOW_HOURS = {"24h": 24, "7d": 7 * 24, "30d": 30 * 24}
_TIME_WINDOW_TO_GNEWS = {"24h": "when:1d", "7d": "when:7d", "30d": "when:30d"}
_TIME_WINDOW_TO_DDG = {"24h": "d", "7d": "w", "30d": "m"}
_TIME_WINDOW_TO_SERP = {"24h": "qdr:d", "7d": "qdr:w", "30d": "qdr:m"}
_TAG_RE = re.compile(r"<[^>]+>")
_RELATIVE_TIME_RE = re.compile(
    r"(?P<count>\d+)\s*(?P<unit>minute|minutes|hour|hours|day|days|week|weeks)\s+ago",
    re.IGNORECASE,
)
_RELATIVE_TIME_ZH_RE = re.compile(r"(?P<count>\d+)\s*(?P<unit>分钟|小时|天|周)前")


def _normalize_time_window(time_window: str) -> str:
    return time_window if time_window in _TIME_WINDOW_HOURS else "24h"


def parse_published_at(value: str, now: datetime | None = None) -> datetime | None:
    """Parse RSS/ISO/relative search-result timestamps into UTC."""
    if not value or not value.strip():
        return None
    now = now or datetime.now(timezone.utc)
    text = value.strip()

    relative = _RELATIVE_TIME_RE.fullmatch(text)
    if relative:
        count = int(relative.group("count"))
        unit = relative.group("unit").lower()
        if unit.startswith("minute"):
            return now - timedelta(minutes=count)
        if unit.startswith("hour"):
            return now - timedelta(hours=count)
        if unit.startswith("day"):
            return now - timedelta(days=count)
        return now - timedelta(weeks=count)

    relative_zh = _RELATIVE_TIME_ZH_RE.fullmatch(text)
    if relative_zh:
        count = int(relative_zh.group("count"))
        unit = relative_zh.group("unit")
        delta = {
            "分钟": timedelta(minutes=count),
            "小时": timedelta(hours=count),
            "天": timedelta(days=count),
            "周": timedelta(weeks=count),
        }[unit]
        return now - delta

    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def filter_fresh_results(
    results: list[dict],
    time_window: str,
    now: datetime | None = None,
    limit: int = 10,
) -> list[dict]:
    """Keep only timestamp-verified results inside the requested UTC window."""
    now = now or datetime.now(timezone.utc)
    time_window = _normalize_time_window(time_window)
    cutoff = now - timedelta(hours=_TIME_WINDOW_HOURS[time_window])
    future_tolerance = now + timedelta(minutes=5)
    deduplicated: dict[str, dict] = {}

    for result in results:
        raw_date = str(result.get("published_at") or result.get("date") or "")
        published_at = parse_published_at(raw_date, now=now)
        if published_at is None or published_at < cutoff or published_at > future_tolerance:
            continue
        title = str(result.get("title") or "").strip()
        if not title:
            continue
        key = re.sub(r"\s+", " ", title).casefold()
        normalized = dict(result)
        normalized["date"] = raw_date
        normalized["published_at"] = published_at.isoformat().replace("+00:00", "Z")
        normalized["age_hours"] = round(max(0.0, (now - published_at).total_seconds() / 3600), 2)
        normalized["freshness_verified"] = True
        existing = deduplicated.get(key)
        if existing is None or normalized["published_at"] > existing["published_at"]:
            deduplicated[key] = normalized

    return sorted(
        deduplicated.values(),
        key=lambda item: item["published_at"],
        reverse=True,
    )[:limit]


class MacroClient:
    """宏观/地缘新闻搜索客户端（自建 search_web tool，免费联网）。"""

    def __init__(self):
        self._provider = config.MACRO_SEARCH_PROVIDER

    async def search_macro_news(self, query: str, time_window: str = "24h") -> dict:
        """联网搜索宏观/地缘政治新闻。

        多级 fallback：Google News RSS → 精简关键词重试 → Tavily/SerpAPI → DuckDuckGo。
        任一源拿到结果即返回；全部失败时返回带 error 的结构（绝不返回假数据）。

        关键修复（实测复现根因）：Google News RSS 是全词精确匹配（AND逻辑），
        关键词越多越难同时命中同一篇文章标题/摘要。实测 "stablecoin depeg USDT USDC risk
        July 2026" 这类 5+ 词 + 冗余年月字面量的查询返回 0 条结果，而精简为 "稳定币 脱锚"
        （2词）后同一时间窗口能命中 23 条真实新闻。因此不完全依赖 LLM 主动生成短查询，
        代码层做兜底：原查询无结果时，自动剥离数字/年月噪音词并截断为核心词重试一次。

        Args:
            query: 搜索关键词
            time_window: 时间窗口 ('24h' | '7d' | '30d')，非法值按 24h 处理
        Returns:
            {"provider", "query", "results": [{title, snippet, source, date, url}], "summary"}
        """
        time_window = _normalize_time_window(time_window)

        # 1. Google News RSS（主源，带可独立校验的发布时间）
        try:
            res = await self._google_news_search(query, time_window)
            if res.get("results") and res.get("freshness_verified"):
                return res
        except Exception as e:
            logger.warning("Google News 搜索失败: %s", e)

        # 1b. 关键词精简重试：原查询词数过多（易触发 AND 匹配全零）时，
        # 剥离年份/月份等噪音词并截断为核心词，用 Google News 再试一次。
        simplified = self._simplify_query(query)
        if simplified and simplified != query:
            try:
                res = await self._google_news_search(simplified, time_window)
                if res.get("results") and res.get("freshness_verified"):
                    logger.info("Google News 原查询 '%s' 无结果，精简为 '%s' 后命中 %d 条", query, simplified, len(res["results"]))
                    res["query"] = query  # 保留原始查询语义，便于上游追溯
                    res["simplified_query"] = simplified
                    return res
            except Exception as e:
                logger.warning("Google News 精简查询搜索失败: %s", e)

        # 2. 可选增强源。只有返回带可验证时间的结果才作为新鲜证据。
        if os.getenv("TAVILY_API_KEY"):
            try:
                res = await self._tavily_search(query, time_window)
                if res.get("results") and res.get("freshness_verified"):
                    return res
            except Exception as e:
                logger.warning("Tavily 搜索失败: %s", e)
        if os.getenv("SERPAPI_API_KEY"):
            try:
                res = await self._serpapi_search(query, time_window)
                if res.get("results") and res.get("freshness_verified"):
                    return res
            except Exception as e:
                logger.warning("SerpAPI 搜索失败: %s", e)

        # 3. DuckDuckGo 无可靠的逐条发布时间，只作为降级背景，不作为方向性证据。
        for candidate in [query, simplified]:
            if not candidate:
                continue
            try:
                res = await self._duckduckgo_search(candidate, time_window)
                if res.get("results"):
                    res["query"] = query
                    if candidate != query:
                        res["simplified_query"] = candidate
                    return res
            except Exception as e:
                logger.warning("DuckDuckGo 搜索失败: %s", e)

        # 全部失败：如实返回失败（不造假），由上层在 caveats/风险中披露。
        return {
            "provider": "none",
            "query": query,
            "results": [],
            "error": "所有联网搜索源均不可达",
            "data_quality": "degraded",
            "time_window": time_window,
            "freshness_verified": False,
            "freshness_basis": "none",
            "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }

    @staticmethod
    def _simplify_query(query: str) -> str:
        """把过长/含噪音词的查询精简为 2-3 个核心词，提升 Google News RSS 命中率。

        策略：
        1. 剥离纯数字 token（年份如 2026）与常见月份英文单词（July 等）——这些是
           when: 时间过滤器已经承担的语义，在关键词里重复只会增加 AND 匹配失败率。
        2. 剥离常见噪音词（risk/analysis/news/latest/最新/风险/分析 等泛化词）。
        3. 剩余词若仍 > 3 个，只保留前 3 个（按原顺序，通常是最具体的实体词）。
        """
        _MONTHS = {
            "january", "february", "march", "april", "may", "june", "july",
            "august", "september", "october", "november", "december",
        }
        _NOISE = {
            "risk", "risks", "analysis", "news", "latest", "update", "report",
            "when", "market", "when:30d", "when:7d", "when:1d",
            "最新", "风险", "分析", "报告", "行情", "市场",
        }
        tokens = query.split()
        kept = []
        for tok in tokens:
            bare = tok.strip().lower()
            if not bare:
                continue
            if bare.isdigit():  # 年份等纯数字
                continue
            if bare in _MONTHS or bare in _NOISE:
                continue
            kept.append(tok)
        if not kept:
            return ""
        return " ".join(kept[:3])

    # ── 主源：Google News RSS ──

    async def _google_news_search(self, query: str, time_window: str) -> dict:
        """Google News RSS 搜索：免费、无需 Key、结果含时间与来源。"""
        time_window = _normalize_time_window(time_window)
        window = _TIME_WINDOW_TO_GNEWS.get(time_window, "when:7d")
        q = quote_plus(f"{query} {window}")
        # Use the configured US/English edition for crypto macro news. The
        # previous CN edition disproportionately returned Chinese domestic
        # finance stories, which are outside this Agent's scope.
        url = (
            f"{GOOGLE_NEWS_RSS}?q={q}"
            f"&hl={quote_plus(config.MACRO_NEWS_LANGUAGE)}"
            f"&gl={quote_plus(config.MACRO_NEWS_REGION)}"
            f"&ceid={quote_plus(config.MACRO_NEWS_EDITION)}"
        )

        async with make_client(timeout=15.0) as http:
            resp = await http.get(url, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            xml_text = resp.text

        now = datetime.now(timezone.utc)
        raw_results = self._parse_rss(xml_text, limit=50)
        results = filter_fresh_results(raw_results, time_window, now=now, limit=10)
        ages = [item["age_hours"] for item in results]
        return {
            "provider": "google_news_rss",
            "query": query,
            "results": results,
            "summary": f"检索到 {len(results)} 条发布时间已验证的新闻（时间窗 {time_window}）",
            "data_quality": "real" if results else "degraded",
            "time_window": time_window,
            "freshness_verified": bool(results),
            "freshness_basis": "published_at",
            "fetched_at": now.isoformat().replace("+00:00", "Z"),
            "freshest_age_hours": min(ages) if ages else None,
            "oldest_age_hours": max(ages) if ages else None,
            "discarded_count": max(0, len(raw_results) - len(results)),
        }

    @staticmethod
    def _parse_rss(xml_text: str, limit: int = 10) -> list[dict]:
        """解析 RSS，提取 title/link/pubDate/source/描述。"""
        results: list[dict] = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return results
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            desc_raw = item.findtext("description") or ""
            # description 里常含 <a> 标签与来源，剥离标签取纯文本
            snippet = unescape(_TAG_RE.sub("", desc_raw)).strip()
            source_el = item.find("source")
            source = source_el.text.strip() if source_el is not None and source_el.text else ""
            if title:
                results.append({
                    "title": title,
                    "snippet": snippet[:300],
                    "source": source,
                    "date": pub,
                    "url": link,
                })
            if len(results) >= limit:
                break
        return results

    # ── 备源：DuckDuckGo Lite ──

    async def _duckduckgo_search(self, query: str, time_window: str) -> dict:
        """DuckDuckGo Lite fallback; results lack verifiable publication timestamps."""
        time_window = _normalize_time_window(time_window)
        async with make_client(timeout=15.0) as http:
            resp = await http.post(
                DUCKDUCKGO_LITE,
                data={"q": query, "kl": config.MACRO_DDG_REGION, "df": _TIME_WINDOW_TO_DDG[time_window]},
                headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            html = resp.text

        # Lite 版为纯 HTML 表格，提取结果链接与标题
        results: list[dict] = []
        # 匹配 <a rel="nofollow" href="URL" class="result-link">TITLE</a>
        for m in re.finditer(r'<a[^>]+class="result-link"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
            url = unescape(m.group(1))
            title = unescape(_TAG_RE.sub("", m.group(2))).strip()
            if title:
                results.append({"title": title, "snippet": "", "source": "", "date": "", "url": url})
            if len(results) >= 10:
                break
        now = datetime.now(timezone.utc)
        return {
            "provider": "duckduckgo",
            "query": query,
            "results": results,
            "summary": f"DuckDuckGo 检索到 {len(results)} 条无可验证发布时间的降级结果",
            "data_quality": "degraded",
            "time_window": time_window,
            "freshness_verified": False,
            "freshness_basis": "provider_filter_unverified",
            "fetched_at": now.isoformat().replace("+00:00", "Z"),
            "caveat": "结果缺少可解析发布时间，不得作为方向性新闻证据",
        }

    # ── 可选增强源 ──

    async def _tavily_search(self, query: str, time_window: str) -> dict:
        api_key = os.getenv("TAVILY_API_KEY", "")
        if not api_key:
            return {"error": "TAVILY_API_KEY 未配置", "provider": "tavily", "results": []}
        time_window = _normalize_time_window(time_window)
        days = _TIME_WINDOW_HOURS[time_window] // 24
        async with make_client(timeout=15.0) as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                json={"api_key": api_key, "query": query, "search_depth": "basic",
                      "max_results": 10, "days": days},
            )
            resp.raise_for_status()
            data = resp.json()
        raw_results = [
            {"title": r.get("title", ""), "snippet": r.get("content", ""),
             "source": r.get("url", ""),
             "date": r.get("published_date") or r.get("published_at") or "",
             "url": r.get("url", "")}
            for r in data.get("results", [])
        ]
        now = datetime.now(timezone.utc)
        results = filter_fresh_results(raw_results, time_window, now=now)
        return {
            "provider": "tavily",
            "query": query,
            "results": results,
            "answer": data.get("answer", ""),
            "data_quality": "real" if results else "degraded",
            "time_window": time_window,
            "freshness_verified": bool(results),
            "freshness_basis": "published_at",
            "fetched_at": now.isoformat().replace("+00:00", "Z"),
            "discarded_count": max(0, len(raw_results) - len(results)),
        }

    async def _serpapi_search(self, query: str, time_window: str) -> dict:
        api_key = os.getenv("SERPAPI_API_KEY", "")
        if not api_key:
            return {"error": "SERPAPI_API_KEY 未配置", "provider": "serpapi", "results": []}
        time_window = _normalize_time_window(time_window)
        async with make_client(timeout=15.0) as client:
            resp = await client.get(
                "https://serpapi.com/search",
                params={
                    "q": query,
                    "api_key": api_key,
                    "num": 10,
                    "tbs": _TIME_WINDOW_TO_SERP[time_window],
                },
            )
            resp.raise_for_status()
            data = resp.json()
        raw_results = [
            {"title": i.get("title", ""), "snippet": i.get("snippet", ""),
             "source": i.get("source", ""), "date": i.get("date", ""), "url": i.get("link", "")}
            for i in data.get("organic_results", [])
        ]
        now = datetime.now(timezone.utc)
        results = filter_fresh_results(raw_results, time_window, now=now)
        return {
            "provider": "serpapi",
            "query": query,
            "results": results,
            "data_quality": "real" if results else "degraded",
            "time_window": time_window,
            "freshness_verified": bool(results),
            "freshness_basis": "published_at",
            "fetched_at": now.isoformat().replace("+00:00", "Z"),
            "discarded_count": max(0, len(raw_results) - len(results)),
        }


macro_client = MacroClient()
