import unittest
from datetime import datetime, timezone

from backend.data.news_graph import assess_government_event, build_news_event_graph


class NewsEventGraphTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)

    def article(self, title, source, age_hours, url):
        return {
            "title": title,
            "source": source,
            "published_at": "2026-07-13T11:00:00Z",
            "age_hours": age_hours,
            "freshness_verified": True,
            "url": url,
        }

    def test_clusters_cross_language_reposts_into_one_confirmed_event(self):
        graph = build_news_event_graph([
            self.article(
                "美联储维持利率不变，释放鹰派信号",
                "财联社",
                1.0,
                "https://a.example/fed",
            ),
            self.article(
                "FOMC holds interest rate, signals hawkish outlook",
                "Reuters",
                1.2,
                "https://b.example/fed",
            ),
            self.article(
                "USDT stablecoin depeg risk rises",
                "CoinDesk",
                2.0,
                "https://c.example/usdt",
            ),
        ], now=self.now)

        self.assertEqual(graph["article_count"], 3)
        self.assertEqual(graph["event_count"], 2)
        self.assertEqual(graph["confirmed_event_count"], 1)
        fed = next(node for node in graph["nodes"] if "federal_reserve" in node["topics"])
        self.assertEqual(fed["article_count"], 2)
        self.assertEqual(fed["independent_source_count"], 2)
        self.assertTrue(fed["cross_source_confirmed"])

    def test_freshness_decay_reduces_older_event_impact(self):
        graph = build_news_event_graph([
            self.article("Bitcoin ETF inflow accelerates", "Source A", 1.0, "https://a.example/etf"),
            self.article("Iran conflict pushes oil price higher", "Source B", 18.0, "https://b.example/oil"),
        ], now=self.now)
        by_title = {node["title"]: node for node in graph["nodes"]}
        self.assertGreater(
            by_title["Bitcoin ETF inflow accelerates"]["freshness_decay"],
            by_title["Iran conflict pushes oil price higher"]["freshness_decay"],
        )

    def test_tags_government_crypto_transfer_for_audit(self):
        graph = build_news_event_graph([
            self.article(
                "US government Bitcoin sale moves to exchange wallet",
                "Reuters",
                1.0,
                "https://a.example/gov-btc",
            ),
        ], now=self.now)
        self.assertEqual(graph["event_count"], 1)
        self.assertIn("government_crypto_sale", graph["nodes"][0]["topics"])

    def test_single_exchange_transfer_is_not_called_a_sale(self):
        graph = build_news_event_graph([
            self.article(
                "US government Bitcoin moved to Coinbase exchange wallet",
                "Source A",
                1.0,
                "https://a.example/gov-btc",
            ),
        ], now=self.now)
        result = assess_government_event(graph["nodes"][0])
        self.assertEqual(result["status"], "unconfirmed_transfer")
        self.assertIn("第二独立新闻源", result["missing_confirmations"])
        self.assertFalse(result["chain_confirmed"])

    def test_two_independent_exchange_reports_raise_probable_sale(self):
        graph = build_news_event_graph([
            self.article(
                "US government Bitcoin moved to Coinbase exchange wallet",
                "Reuters",
                1.0,
                "https://a.example/gov-btc",
            ),
            self.article(
                "US government transfers BTC to Coinbase",
                "Bloomberg",
                1.2,
                "https://b.example/gov-btc",
            ),
        ], now=self.now)
        result = assess_government_event(graph["nodes"][0])
        self.assertEqual(result["status"], "probable_sale")
        self.assertTrue(result["multi_source_confirmed"])
        self.assertIn("第二独立新闻源", result["confirmed_by"])

    def test_chain_deposit_plus_exchange_claim_confirms_transfer(self):
        graph = build_news_event_graph([
            self.article(
                "US government Ethereum deposited to Binance exchange wallet",
                "Reuters",
                1.0,
                "https://a.example/gov-eth",
            ),
        ], now=self.now)
        result = assess_government_event(graph["nodes"][0], chain_deposit_count=1)
        self.assertEqual(result["status"], "confirmed_sale")
        self.assertTrue(result["chain_confirmed"])
        self.assertIn("政府钱包→交易所链上入金", result["confirmed_by"])

    def test_official_auction_is_announcement_not_completed_sale(self):
        graph = build_news_event_graph([
            self.article(
                "US Marshals announces Bitcoin auction",
                "US Marshals",
                1.0,
                "https://a.example/auction",
            ),
        ], now=self.now)
        result = assess_government_event(graph["nodes"][0])
        self.assertEqual(result["status"], "auction_announced")
        self.assertTrue(result["official_auction"])
        self.assertNotEqual(result["status"], "confirmed_sale")

    def test_multilingual_government_transfer_reports_cluster_as_one_event(self):
        graph = build_news_event_graph([
            self.article(
                "Government transfers BTC to Coinbase",
                "Moomoo",
                1.0,
                "https://a.example/gov-btc",
            ),
            self.article(
                "美国政府向 Coinbase Prime 转入2874.9枚比特币",
                "DigitalToday",
                1.2,
                "https://b.example/gov-btc",
            ),
        ], now=self.now)
        government = [
            node for node in graph["nodes"]
            if "government_transfer" in node["topics"] or "government_crypto_sale" in node["topics"]
        ]
        self.assertEqual(len(government), 1)
        self.assertEqual(government[0]["article_count"], 2)
        self.assertEqual(government[0]["independent_source_count"], 2)

    def test_unrelated_non_generic_topics_are_not_merged_by_title_overlap(self):
        graph = build_news_event_graph([
            self.article("Iran oil conflict escalates", "Source A", 1.0, "https://a.example/iran"),
            self.article("Iran stablecoin regulation update", "Source B", 1.2, "https://b.example/stablecoin"),
        ], now=self.now)
        self.assertEqual(graph["event_count"], 2)


if __name__ == "__main__":
    unittest.main()
