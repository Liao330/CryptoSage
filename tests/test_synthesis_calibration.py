import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.agents.critic import run_critic
from backend.agents.synthesis import (
    _apply_confidence_guard,
    _calibrate_synthesis_result,
    _compact_evidence_for_prompt,
)


def signal(agent, bias, score, confidence, quality="real"):
    return {
        "agent": agent,
        "bias": bias,
        "score": score,
        "confidence": confidence,
        "data_quality": quality,
    }


class SynthesisCalibrationTests(unittest.TestCase):
    def setUp(self):
        # 校准逻辑的基线是五维框架；数据源开关（.env 的 ENABLE_MACRO_AGENT）
        # 属于运行时部署配置，不应影响单元测试的确定性
        self._macro_patcher = patch("backend.agents.synthesis._MACRO_ENABLED", True)
        self._macro_patcher.start()
        self.addCleanup(self._macro_patcher.stop)

    def test_mixed_low_confidence_evidence_keeps_bearish_direction(self):
        evidence = [
            signal("onchain", "neutral", 50, 0.25),
            signal("sentiment", "bullish", 65, 0.55),
            signal("derivatives", "bearish", 38, 0.55),
            signal("technical", "neutral", 42, 0.70),
            signal("macro", "bearish", 28, 0.72),
        ]
        result = _calibrate_synthesis_result(
            {"direction": "bearish", "confidence": 0.44, "market_state": "高波动/事件驱动"},
            evidence,
        )
        result = _apply_confidence_guard(result, evidence)

        self.assertEqual(result["direction"], "bearish")
        self.assertLess(result["confidence"], 0.60)
        self.assertEqual(result["position_advice"]["action"], "观望（自动）")
        self.assertLess(result["direction_score"], 50)
        self.assertEqual(result["confidence_calibration"]["data_coverage"], 1.0)

    def test_single_directional_agent_keeps_forecast_but_is_not_executable(self):
        evidence = [
            signal("macro", "bearish", 20, 0.85),
            signal("technical", "neutral", 45, 0.70),
            signal("sentiment", "neutral", 50, 0.60),
            signal("onchain", "neutral", 50, 0.30, quality="partial"),
        ]
        result = _calibrate_synthesis_result(
            {"direction": "bearish", "confidence": 0.8, "market_state": "地缘危机"},
            evidence,
        )
        result = _apply_confidence_guard(result, evidence)
        self.assertEqual(result["direction"], "bearish")
        self.assertEqual(result["forecast_mode"], "directional")
        self.assertEqual(result["execution_mode"], "observe")
        self.assertTrue(result["confidence_calibration"]["directional_evidence_weak"])
        self.assertLessEqual(result["confidence"], 0.54)
        self.assertEqual(result["position_advice"]["action"], "观望（自动）")
        self.assertEqual(result["confidence_calibration"]["aligned_agent_count"], 1)

    def test_partial_dimensions_reduce_coverage_and_agreement(self):
        evidence = [
            signal("macro", "bearish", 20, 0.80),
            signal("technical", "bearish", 35, 0.70),
            {**signal("onchain", "neutral", 50, 0.30, quality="partial"), "raw_metrics": {"coverage_score": 0.35}},
            {**signal("derivatives", "neutral", 50, 0.25, quality="partial"), "raw_metrics": {"coverage_score": 0.60}},
        ]
        result = _calibrate_synthesis_result(
            {"direction": "bearish", "confidence": 0.8, "market_state": "趋势市"},
            evidence,
        )
        self.assertLess(result["confidence_calibration"]["data_coverage"], 1.0)

    def test_balanced_directional_evidence_is_truly_neutral(self):
        evidence = [
            signal("technical", "bullish", 65, 0.70),
            signal("derivatives", "bearish", 35, 0.70),
        ]
        result = _calibrate_synthesis_result(
            {"direction": "bullish", "confidence": 0.8, "market_state": "趋势市"},
            evidence,
        )

        self.assertEqual(result["direction"], "neutral")
        self.assertGreaterEqual(result["direction_score"], 46.0)
        self.assertLessEqual(result["direction_score"], 54.0)

    def test_one_real_dimension_keeps_low_confidence_forecast(self):
        evidence = [
            signal("technical", "bearish", 30, 0.70),
            signal("derivatives", "bearish", 25, 0.60, quality="degraded"),
        ]
        result = _apply_confidence_guard(
            {"direction": "bearish", "confidence": 0.75},
            evidence,
        )

        self.assertEqual(result["direction"], "bearish")
        self.assertLessEqual(result["confidence"], 0.35)
        self.assertEqual(result["forecast_mode"], "directional")
        self.assertEqual(result["execution_mode"], "observe")
        self.assertEqual(result["position_advice"]["action"], "观望（自动）")

    def test_no_usable_dimensions_still_forces_neutral(self):
        evidence = [
            signal("technical", "bearish", 30, 0.70, quality="degraded"),
            signal("derivatives", "bullish", 70, 0.60, quality="degraded"),
        ]
        result = _apply_confidence_guard(
            {"direction": "bearish", "confidence": 0.75},
            evidence,
        )

        self.assertEqual(result["direction"], "neutral")
        self.assertEqual(result["confidence"], 0.0)

    def test_timeframes_can_express_short_bullish_and_long_bearish(self):
        evidence = [
            signal("technical", "bullish", 80, 0.80),
            signal("derivatives", "bullish", 65, 0.70),
            signal("macro", "bearish", 20, 0.80),
            signal("onchain", "bearish", 35, 0.60),
            signal("sentiment", "neutral", 50, 0.50),
        ]
        result = _calibrate_synthesis_result(
            {"direction": "neutral", "confidence": 0.5, "market_state": "震荡市"},
            evidence,
        )

        self.assertEqual(result["timeframe_outlook"]["4H"]["direction"], "bullish")
        self.assertEqual(result["timeframe_outlook"]["7D"]["direction"], "bearish")

    def test_timeframe_outlook_contains_distinct_horizon_context(self):
        evidence = [
            signal("technical", "bullish", 70, 0.70),
            signal("derivatives", "bullish", 62, 0.65),
            signal("onchain", "neutral", 50, 0.60),
            signal("macro", "neutral", 50, 0.55),
        ]
        result = _calibrate_synthesis_result(
            {"direction": "bullish", "confidence": 0.6, "market_state": "趋势市"},
            evidence,
        )
        outlook = result["timeframe_outlook"]
        self.assertEqual(outlook["4H"]["label"], "短线执行窗口")
        self.assertEqual(outlook["7D"]["label"], "波段观察窗口")
        self.assertNotEqual(outlook["4H"]["thesis"], outlook["7D"]["thesis"])
        self.assertGreater(outlook["4H"]["weight_profile"]["technical"], outlook["7D"]["weight_profile"]["technical"])

    def test_report_keeps_empty_news_graph_visible_when_macro_is_missing(self):
        result = _calibrate_synthesis_result(
            {"direction": "bullish", "confidence": 0.6, "market_state": "趋势市"},
            [signal("technical", "bullish", 70, 0.70), signal("derivatives", "bullish", 62, 0.65)],
        )
        self.assertEqual(result["news_event_graph"]["event_count"], 0)
        self.assertEqual(result["news_event_graph"]["nodes"], [])

    def test_counterfactuals_are_recomputed_and_ranked(self):
        evidence = [
            signal("technical", "neutral", 48, 0.60),
            signal("derivatives", "bearish", 35, 0.65),
            signal("macro", "bearish", 20, 0.80),
            signal("sentiment", "bullish", 62, 0.55),
        ]
        result = _calibrate_synthesis_result(
            {
                "direction": "bearish",
                "confidence": 0.6,
                "market_state": "高波动/事件驱动",
                "key_levels": {},
            },
            evidence,
        )

        self.assertTrue(result["counterfactuals"])
        self.assertEqual(result["counterfactuals"][0]["agent"], "macro")
        self.assertGreater(result["counterfactuals"][0]["score_change"], 0)
        self.assertIn("result_score", result["counterfactuals"][0])

    def test_historical_multipliers_change_effective_agent_weights(self):
        evidence = [
            signal("technical", "bullish", 80, 0.70),
            signal("macro", "bearish", 20, 0.70),
        ]
        result = _calibrate_synthesis_result(
            {"direction": "neutral", "confidence": 0.5, "market_state": "未确定"},
            evidence,
            weight_multipliers={"technical": 0.70, "macro": 1.30},
        )

        self.assertGreater(result["weighted_scores"]["macro"], result["weighted_scores"]["technical"])
        self.assertEqual(
            result["confidence_calibration"]["dynamic_weight_multipliers"]["macro"],
            1.30,
        )

    def test_prompt_evidence_excludes_traces_and_article_replicas(self):
        compact = _compact_evidence_for_prompt([{
            "agent": "macro",
            "bias": "bearish",
            "score": 30,
            "thinking": "very long hidden trace",
            "news_event_graph": {
                "article_count": 2,
                "event_count": 1,
                "confirmed_event_count": 1,
                "nodes": [{
                    "event_id": "evt_1",
                    "title": "Policy event",
                    "impact_score": 0.8,
                    "articles": [{"title": "replica A"}, {"title": "replica B"}],
                }],
            },
        }])

        self.assertNotIn("thinking", compact[0])
        self.assertNotIn("articles", compact[0]["news_event_graph"]["nodes"][0])
        self.assertEqual(compact[0]["news_event_graph"]["article_count"], 2)


class CriticConfidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_critic_penalty_is_capped_and_applied_only_once(self):
        critique = json.dumps({
            "has_valid_critique": True,
            "critiques": [{"point": "存在反向证据", "severity": "high"}],
            "adjusted_confidence": 0.10,
            "confidence_adjustment": -0.50,
        })
        response = SimpleNamespace(choices=[SimpleNamespace(
            reasoning_content="",
            message=SimpleNamespace(content=critique, reasoning_content=""),
        )])
        state = {
            "synthesis_result": {
                "direction": "bearish",
                "confidence": 0.55,
                "confidence_calibration": {},
            },
            "evidence_pool": [],
            "critic_round": 0,
            "critic_feedback": [],
            "trace": [],
        }

        with patch("backend.agents.critic.hy3_client.achat", AsyncMock(return_value=response)):
            state = await run_critic(state)
            first_confidence = state["synthesis_result"]["confidence"]
            state = await run_critic(state)

        self.assertEqual(first_confidence, 0.45)
        self.assertEqual(state["synthesis_result"]["confidence"], 0.45)
        self.assertEqual(state["_critic_confidence_penalty"], -0.10)


if __name__ == "__main__":
    unittest.main()
