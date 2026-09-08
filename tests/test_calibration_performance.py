import json
import os
import tempfile
import unittest
from unittest.mock import patch

import aiosqlite

from backend.calibration.performance import (
    calculate_shadow_metrics,
    get_agent_drift_report,
    get_agent_performance,
)
from backend.config import config
from backend.data.db import init_db


class AgentPerformanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "calibration.db")
        self.db_path_patch = patch.object(config, "DB_PATH", self.db_path)
        self.db_path_patch.start()
        await init_db()

    async def asyncTearDown(self):
        self.db_path_patch.stop()
        self.temp_dir.cleanup()

    async def insert_sample(self, task_id, evidence, actual_up, resolved_at):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO analysis_history
                   (task_id, symbol, direction, confidence, evidence_pool)
                   VALUES (?, 'BTC-USDT', 'bullish', 0.7, ?)""",
                (task_id, json.dumps(evidence)),
            )
            await db.execute(
                """INSERT INTO backtest_tracks
                   (task_id, symbol, direction, confidence, entry_price,
                    exit_price, pnl_pct, status, resolved_at)
                   VALUES (?, 'BTC-USDT', 'bullish', 0.7, 100, ?, ?, 'resolved', ?)""",
                (task_id, 110 if actual_up else 90, 10 if actual_up else -10, resolved_at),
            )
            await db.commit()

    async def test_resolved_outcomes_raise_good_agent_and_lower_bad_agent(self):
        evidence = [
            {"agent": "technical", "bias": "bullish", "score": 80},
            {"agent": "macro", "bias": "bearish", "score": 20},
        ]
        for index in range(20):
            await self.insert_sample(
                f"perf-{index}",
                evidence,
                actual_up=True,
                resolved_at=f"2026-07-{index + 1:02d} 12:00:00",
            )

        report = await get_agent_performance("BTC-USDT")
        technical = report["agents"]["technical"]
        macro = report["agents"]["macro"]
        self.assertEqual(technical["sample_count"], 20)
        self.assertEqual(technical["accuracy"], 1.0)
        self.assertGreater(technical["weight_multiplier"], 1.0)
        self.assertEqual(macro["accuracy"], 0.0)
        self.assertLess(macro["weight_multiplier"], 1.0)

    async def test_recent_accuracy_collapse_triggers_drift_alert(self):
        evidence = [{"agent": "technical", "bias": "bullish", "score": 80}]
        for index in range(6):
            await self.insert_sample(
                f"baseline-{index}", evidence, True, f"2026-06-{index + 1:02d} 12:00:00"
            )
        for index in range(4):
            await self.insert_sample(
                f"recent-{index}", evidence, False, f"2026-07-{index + 1:02d} 12:00:00"
            )

        report = await get_agent_drift_report(
            "BTC-USDT", recent_window=4, baseline_window=6, min_recent=3, min_baseline=5
        )
        self.assertEqual(report["agents"]["technical"]["status"], "degrading")
        self.assertEqual(report["agents"]["technical"]["accuracy_delta"], -1.0)
        self.assertEqual(report["alerts"][0]["agent"], "technical")


class ShadowMetricsTests(unittest.TestCase):
    def test_shadow_metrics_include_cost_aware_risk_statistics(self):
        metrics = calculate_shadow_metrics([
            {"status": "resolved", "pnl_pct": 3.0},
            {"status": "resolved", "pnl_pct": -1.0},
            {"status": "resolved", "pnl_pct": 2.0},
            {"status": "tracking", "pnl_pct": None},
        ])
        self.assertEqual(metrics["resolved_count"], 3)
        self.assertEqual(metrics["win_count"], 2)
        self.assertEqual(metrics["cumulative_pnl_pct"], 4.0)
        self.assertEqual(metrics["profit_factor"], 5.0)
        self.assertEqual(metrics["max_drawdown_pct"], -1.0)


if __name__ == "__main__":
    unittest.main()
