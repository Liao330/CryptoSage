import unittest
from unittest.mock import AsyncMock, patch

import pandas as pd

from backend.data.derivatives_client import DerivativesClient, _funding_interpretation, summarize_options_market
from backend.agents.derivatives import _merge_options_signal
from backend.data.kline_repository import aggregate_price_samples
from backend.indicators.ta import calc_rsi


class RsiTests(unittest.TestCase):
    def test_monotonic_uptrend_is_overbought(self):
        frame = pd.DataFrame({"Close": list(range(1, 31))})
        self.assertEqual(calc_rsi(frame)["value"], 100.0)

    def test_monotonic_downtrend_is_oversold(self):
        frame = pd.DataFrame({"Close": list(range(30, 0, -1))})
        self.assertEqual(calc_rsi(frame)["value"], 0.0)

    def test_flat_market_is_neutral(self):
        frame = pd.DataFrame({"Close": [10] * 30})
        self.assertEqual(calc_rsi(frame)["value"], 50.0)


class FundingRateTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_gate_result_continues_to_fallback(self):
        client = DerivativesClient()
        gate_result = {
            "symbol": "BTCUSDT",
            "error": "no data",
            "source": "gate",
            "data_quality": "degraded",
        }
        okx_result = {
            "symbol": "BTCUSDT",
            "funding_rate": 0.001,
            "source": "okx",
            "data_quality": "real",
        }
        with (
            patch.object(client, "_get_funding_rate_gate", AsyncMock(return_value=gate_result)),
            patch.object(client, "_get_funding_rate_coingecko", AsyncMock(return_value=okx_result)) as fallback,
            patch("backend.data.derivatives_client.make_client", side_effect=RuntimeError("offline")),
        ):
            result = await client.get_funding_rate("BTCUSDT")

        self.assertEqual(result, okx_result)
        fallback.assert_awaited_once_with("BTCUSDT")

    def test_payment_direction_matches_rate_sign(self):
        self.assertIn("\u591a\u5934\u652f\u4ed8\u7a7a\u5934", _funding_interpretation(0.001))
        self.assertIn("\u7a7a\u5934\u652f\u4ed8\u591a\u5934", _funding_interpretation(-0.001))


class OptionsMarketTests(unittest.TestCase):
    def setUp(self):
        self.now_ms = 1_750_000_000_000

    def test_near_expiry_max_pain_is_exposed_as_pinning_risk(self):
        instruments = [
            {"instrument_name": "BTC-EXP-60000-C", "expiration_timestamp": self.now_ms + 24 * 3600_000, "strike": 60000, "option_type": "call"},
            {"instrument_name": "BTC-EXP-60000-P", "expiration_timestamp": self.now_ms + 24 * 3600_000, "strike": 60000, "option_type": "put"},
            {"instrument_name": "BTC-EXP-65000-C", "expiration_timestamp": self.now_ms + 24 * 3600_000, "strike": 65000, "option_type": "call"},
            {"instrument_name": "BTC-EXP-65000-P", "expiration_timestamp": self.now_ms + 24 * 3600_000, "strike": 65000, "option_type": "put"},
        ]
        summaries = [
            {"instrument_name": "BTC-EXP-60000-C", "open_interest": 10, "volume": 1, "underlying_price": 60000},
            {"instrument_name": "BTC-EXP-60000-P", "open_interest": 10, "volume": 1, "underlying_price": 60000},
            {"instrument_name": "BTC-EXP-65000-C", "open_interest": 1, "volume": 1, "underlying_price": 60000},
            {"instrument_name": "BTC-EXP-65000-P", "open_interest": 1, "volume": 1, "underlying_price": 60000},
        ]
        result = summarize_options_market(instruments, summaries, now_ms=self.now_ms)
        self.assertEqual(result["data_quality"], "real")
        self.assertEqual(result["max_pain"], 60000)
        self.assertEqual(result["positioning_mode"], "pinning")
        self.assertEqual(result["bias"], "neutral")

    def test_option_oi_does_not_create_high_confidence_direction(self):
        expiry = self.now_ms + 48 * 3600_000
        instruments = [
            {"instrument_name": "BTC-EXP-65000-C", "expiration_timestamp": expiry, "strike": 65000, "option_type": "call"},
            {"instrument_name": "BTC-EXP-60000-P", "expiration_timestamp": expiry, "strike": 60000, "option_type": "put"},
        ]
        summaries = [
            {"instrument_name": "BTC-EXP-65000-C", "open_interest": 100, "volume": 10, "underlying_price": 60000},
            {"instrument_name": "BTC-EXP-60000-P", "open_interest": 10, "volume": 2, "underlying_price": 60000},
        ]
        result = summarize_options_market(instruments, summaries, now_ms=self.now_ms)
        self.assertIn(result["bias"], {"bullish", "neutral"})
        self.assertLessEqual(result["confidence"], 0.35)
        self.assertIn("做市商", result["interpretation"])

    def test_options_can_support_a_degraded_perpetual_signal_without_overriding_it(self):
        signal = {
            "bias": "neutral",
            "score": 50,
            "confidence": 0.2,
            "data_quality": "degraded",
            "evidence": [],
            "caveats": [],
        }
        _merge_options_signal(signal, {
            "data_quality": "real",
            "bias": "bullish",
            "score": 56,
            "confidence": 0.35,
            "hours_to_expiry": 24,
            "max_pain": 65000,
            "call_put_oi_ratio": 1.8,
            "interpretation": "Call墙位于现价上方",
            "caveat": "买卖方未知",
        })
        self.assertEqual(signal["data_quality"], "real")
        self.assertEqual(signal["bias"], "bullish")
        self.assertLessEqual(signal["confidence"], 0.45)
        self.assertTrue(any("期权交割" in item for item in signal["evidence"]))


class DeribitNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_unreachable_deribit_returns_actionable_network_diagnostics(self):
        client = DerivativesClient()
        with patch(
            "backend.data.derivatives_client.make_client",
            side_effect=RuntimeError("[SSL: RECORD_LAYER_FAILURE] record layer failure"),
        ):
            result = await client.get_options_snapshot("BTC-USDT")
        self.assertEqual(result["data_quality"], "degraded")
        self.assertEqual(result["proxy_configured"], False)
        self.assertIn("DERIBIT_PROXY_URL", result["network_hint"])
        self.assertIn("RuntimeError", result["error_types"])


class KlineAggregationTests(unittest.TestCase):
    def test_uses_only_samples_from_each_requested_bucket(self):
        hour = 3_600_000
        prices = [
            [0, 100.0],
            [hour, 110.0],
            [4 * hour, 80.0],
            [5 * hour, 90.0],
        ]
        volumes = [[item[0], 1_000.0 + index] for index, item in enumerate(prices)]

        result = aggregate_price_samples("BTC-USDT", "4H", prices, volumes)

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["bar"], "4H")
        self.assertEqual(result[0]["high"], 110.0)
        self.assertEqual(result[0]["low"], 100.0)
        self.assertEqual(result[1]["high"], 90.0)
        self.assertEqual(result[1]["low"], 80.0)
        self.assertEqual(result[0]["data_quality"], "degraded")


if __name__ == "__main__":
    unittest.main()
