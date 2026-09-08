import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from backend.agents.macro import _search_current_news, run_macro_agent
from backend.data.macro_client import MacroClient, filter_fresh_results, parse_published_at


class PublishedAtTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc)

    def test_parses_rfc_iso_and_relative_dates(self):
        self.assertEqual(
            parse_published_at("Mon, 13 Jul 2026 08:00:00 GMT", self.now),
            datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            parse_published_at("2026-07-13T07:00:00Z", self.now),
            datetime(2026, 7, 13, 7, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(parse_published_at("2 hours ago", self.now), self.now - timedelta(hours=2))

    def test_filters_stale_missing_future_and_duplicate_results(self):
        results = [
            {"title": "Fresh", "date": "2 hours ago", "url": "https://a"},
            {"title": "fresh", "date": "1 hour ago", "url": "https://b"},
            {"title": "Stale", "date": "2 days ago", "url": "https://c"},
            {"title": "Missing", "date": "", "url": "https://d"},
            {"title": "Future", "date": "2026-07-14T09:00:00Z", "url": "https://e"},
        ]

        filtered = filter_fresh_results(results, "24h", now=self.now)

        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["url"], "https://b")
        self.assertEqual(filtered[0]["age_hours"], 1.0)
        self.assertTrue(filtered[0]["freshness_verified"])

    def test_invalid_window_falls_back_to_24_hours(self):
        results = [{"title": "Old", "date": "2 days ago", "url": "https://a"}]
        self.assertEqual(filter_fresh_results(results, "invalid", now=self.now), [])


class MacroAgentFreshnessGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_forces_model_requested_window_to_24_hours(self):
        search_result = {"provider": "google_news_rss", "results": []}
        with patch(
            "backend.agents.macro.macro_client.search_macro_news",
            AsyncMock(return_value=search_result),
        ) as search:
            result = await _search_current_news("FOMC 利率", "30d")

        search.assert_awaited_once_with("FOMC 利率", "24h")
        self.assertEqual(result["requested_time_window"], "30d")
        self.assertEqual(result["effective_time_window"], "24h")

    async def test_unverified_search_cannot_create_directional_signal(self):
        model_output = json.dumps({
            "bias": "bullish",
            "score": 80,
            "confidence": 0.9,
            "evidence": ["unverified claim"],
            "key_events": [{"event": "claim"}],
        })
        tool_result = {
            "provider": "duckduckgo",
            "results": [{"title": "No timestamp", "freshness_verified": False}],
            "data_quality": "degraded",
            "freshness_verified": False,
            "fetched_at": "2026-07-13T09:00:00Z",
        }
        fc_result = {
            "final_message": model_output,
            "reasoning_content": "",
            "tool_calls_log": [{"function": "search_macro_news", "result": tool_result}],
        }

        with patch("backend.agents.macro.run_function_calling", AsyncMock(return_value=fc_result)):
            state = await run_macro_agent({"symbol": "BTC-USDT", "query": "test", "trace": []})

        signal = state["evidence_pool"][0]
        self.assertEqual(signal["bias"], "neutral")
        self.assertEqual(signal["score"], 50)
        self.assertLessEqual(signal["confidence"], 0.3)
        self.assertEqual(signal["evidence"], [])
        self.assertEqual(signal["data_quality"], "degraded")

    async def test_verified_news_becomes_deterministic_evidence(self):
        model_output = json.dumps({"bias": "bearish", "score": 30, "confidence": 0.6})
        news = {
            "title": "Policy update",
            "source": "Example News",
            "published_at": "2026-07-13T08:00:00Z",
            "age_hours": 1.0,
            "url": "https://example.com/news",
            "freshness_verified": True,
        }
        fc_result = {
            "final_message": model_output,
            "reasoning_content": "",
            "tool_calls_log": [{
                "function": "search_macro_news",
                "result": {
                    "provider": "google_news_rss",
                    "results": [news],
                    "data_quality": "real",
                    "freshness_verified": True,
                    "fetched_at": "2026-07-13T09:00:00Z",
                },
            }],
        }

        with patch("backend.agents.macro.run_function_calling", AsyncMock(return_value=fc_result)):
            state = await run_macro_agent({"symbol": "BTC-USDT", "query": "test", "trace": []})

        signal = state["evidence_pool"][0]
        self.assertEqual(signal["data_quality"], "real")
        self.assertIn("Policy update", signal["evidence"][0])
        self.assertEqual(signal["raw_metrics"]["freshest_age_hours"], 1.0)

    async def test_week_old_verified_news_cannot_create_directional_signal(self):
        model_output = json.dumps({"bias": "bullish", "score": 80, "confidence": 0.9})
        old_news = {
            "title": "Week-old policy update",
            "source": "Example News",
            "published_at": "2026-07-11T09:00:00Z",
            "age_hours": 48.0,
            "url": "https://example.com/old-news",
            "freshness_verified": True,
        }
        fc_result = {
            "final_message": model_output,
            "reasoning_content": "",
            "tool_calls_log": [{
                "function": "search_macro_news",
                "result": {
                    "provider": "google_news_rss",
                    "results": [old_news],
                    "freshness_verified": True,
                },
            }],
        }

        with patch("backend.agents.macro.run_function_calling", AsyncMock(return_value=fc_result)):
            state = await run_macro_agent({"symbol": "BTC-USDT", "query": "test", "trace": []})

        signal = state["evidence_pool"][0]
        self.assertEqual(signal["bias"], "neutral")
        self.assertEqual(signal["score"], 50)
        self.assertEqual(signal["evidence"], [])
        self.assertEqual(signal["raw_metrics"]["effective_time_window"], "24h")

    async def test_priority_queries_and_china_filter_keep_crypto_us_policy_news(self):
        model_output = json.dumps({"bias": "bullish", "score": 70, "confidence": 0.7})
        model_result = {
            "final_message": model_output,
            "reasoning_content": "",
            "tool_calls_log": [],
        }

        def search_result(query, _window):
            title = "Bitcoin ETF inflows lift crypto market"
            if query == "government Bitcoin sale":
                title = "US government Bitcoin moved to Coinbase"
            elif query in {"bitcoin crypto market", "bitcoin ethereum crypto"}:
                title = "China domestic crypto policy update"
            elif query == "FOMC interest rates":
                title = "Federal Reserve holds interest rates"
            return {
                "provider": "google_news_rss",
                "query": query,
                "results": [{
                    "title": title,
                    "source": "Reuters",
                    "published_at": "2026-07-13T08:00:00Z",
                    "age_hours": 1.0,
                    "url": "https://example.com/news",
                    "freshness_verified": True,
                }],
                "freshness_verified": True,
                "fetched_at": "2026-07-13T09:00:00Z",
            }

        with (
            patch("backend.agents.macro.run_function_calling", AsyncMock(return_value=model_result)),
            patch("backend.agents.macro._search_current_news", AsyncMock(side_effect=search_result)) as search,
        ):
            state = await run_macro_agent({"symbol": "BTC-USDT", "query": "test", "trace": []})

        signal = state["evidence_pool"][0]
        self.assertGreater(search.await_count, 0)
        self.assertEqual(signal["raw_metrics"]["news_focus_profile"], "crypto_market_and_us_policy")
        self.assertGreater(signal["raw_metrics"]["supplemental_search_count"], 0)
        self.assertGreater(signal["raw_metrics"]["filtered_china_result_count"], 0)
        self.assertIn("Federal Reserve holds interest rates", " ".join(signal["evidence"]))
        self.assertNotIn("China domestic crypto policy update", " ".join(signal["evidence"]))

    async def test_google_news_uses_us_english_edition(self):
        class Response:
            text = "<rss><channel></channel></rss>"

            def raise_for_status(self):
                return None

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, url, **_kwargs):
                self.url = url
                return Response()

        client = Client()
        with patch("backend.data.macro_client.make_client", return_value=client):
            await MacroClient()._google_news_search("FOMC interest rates", "24h")

        self.assertIn("hl=en-US", client.url)
        self.assertIn("gl=US", client.url)
        self.assertIn("ceid=US%3Aen", client.url)


if __name__ == "__main__":
    unittest.main()
