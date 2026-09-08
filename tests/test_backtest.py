import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import aiosqlite

from backend import main
from backend.config import config
from backend.data.db import get_analysis_by_task_id, init_db, save_analysis_history


class BacktestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test.db")
        self.db_path_patch = patch.object(config, "DB_PATH", self.db_path)
        self.db_path_patch.start()

    async def asyncTearDown(self):
        self.db_path_patch.stop()
        self.temp_dir.cleanup()

    async def test_migrates_old_table_and_only_resolves_matured_rows(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """CREATE TABLE backtest_tracks (
                       task_id TEXT PRIMARY KEY,
                       symbol TEXT NOT NULL,
                       direction TEXT NOT NULL,
                       confidence REAL,
                       entry_price REAL,
                       exit_price REAL,
                       pnl_pct REAL,
                       status TEXT DEFAULT 'tracking',
                       created_at TEXT DEFAULT (datetime('now')),
                       resolved_at TEXT
                   )"""
            )
            await db.execute(
                """INSERT INTO backtest_tracks
                   (task_id, symbol, direction, confidence, entry_price, created_at)
                   VALUES ('old', 'BTC-USDT', 'bullish', 0.7, 100, datetime('now', '-2 days'))"""
            )
            await db.executemany(
                """INSERT INTO backtest_tracks
                   (task_id, symbol, direction, confidence, entry_price, created_at)
                   VALUES (?, 'BTC-USDT', ?, ?, 100, datetime('now', '-2 days'))""",
                [
                    ("legacy-neutral", "neutral", 0.8),
                    ("legacy-low", "bearish", 0.4),
                ],
            )
            await db.commit()

        with patch.object(config, "BACKTEST_HORIZON_HOURS", 24):
            await init_db()

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("PRAGMA table_info(backtest_tracks)")
            columns = {row[1] for row in await cursor.fetchall()}
            self.assertIn("target_at", columns)
            self.assertIn("direction_score", columns)
            self.assertIn("action", columns)
            self.assertIn("calibration", columns)
            self.assertIn("track_type", columns)
            await db.execute(
                """INSERT INTO backtest_tracks
                   (task_id, symbol, direction, confidence, entry_price, target_at)
                   VALUES ('future', 'BTC-USDT', 'bullish', 0.7, 100, datetime('now', '+1 day'))"""
            )
            await db.commit()

        current_kline = [{"close": 110.0, "data_quality": "real"}]
        with (
            patch.object(main.kline_repo, "get_klines", AsyncMock(return_value=current_kline)),
            patch.object(config, "BACKTEST_ROUND_TRIP_COST_BPS", 10.0),
        ):
            result = await main.resolve_backtest("BTC-USDT")

        self.assertEqual(result["resolved_count"], 1)
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT task_id, status, pnl_pct FROM backtest_tracks ORDER BY task_id"
            )
            rows = {row[0]: (row[1], row[2]) for row in await cursor.fetchall()}
        self.assertEqual(rows["old"], ("resolved", 9.9))
        self.assertEqual(rows["future"], ("tracking", None))
        self.assertEqual(rows["legacy-neutral"], ("ignored", None))
        self.assertEqual(rows["legacy-low"], ("ignored", None))

    async def test_directional_observe_and_executable_forecasts_create_tracks(self):
        await init_db()
        current_kline = [{"close": 100.0, "data_quality": "real"}]
        with patch.object(
            main.kline_repo,
            "get_klines",
            AsyncMock(return_value=current_kline),
        ) as get_klines:
            await main._create_backtest_track(
                "observe",
                "BTC-USDT",
                {
                    "direction": "bearish",
                    "confidence": 0.49,
                    "position_advice": {"action": "观望（自动）"},
                },
                [],
            )
            await main._create_backtest_track(
                "execute",
                "BTC-USDT",
                {
                    "direction": "bullish",
                    "confidence": 0.65,
                    "position_advice": {"action": "轻仓试探"},
                },
                [],
            )

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
            "SELECT task_id, direction, confidence, track_type, action FROM backtest_tracks ORDER BY task_id"
            )
            rows = await cursor.fetchall()
        self.assertEqual(rows, [
            ("execute", "bullish", 0.65, "execute", "轻仓试探"),
            ("observe", "bearish", 0.49, "observe", "观望（自动）"),
        ])
        self.assertEqual(get_klines.await_count, 2)

    async def test_background_settlement_only_calls_matured_symbols(self):
        await init_db()
        async with aiosqlite.connect(self.db_path) as db:
            await db.executemany(
                """INSERT INTO backtest_tracks
                   (task_id, symbol, direction, confidence, entry_price, target_at, status)
                   VALUES (?, ?, 'bullish', 0.7, 100, ?, 'tracking')""",
                [
                    ("btc-due", "BTC-USDT", "2020-01-01 00:00:00"),
                    ("eth-future", "ETH-USDT", "2099-01-01 00:00:00"),
                ],
            )
            await db.commit()

        with patch("backend.main.resolve_backtest", AsyncMock(return_value={"resolved_count": 1})) as resolve:
            results = await main._resolve_due_shadow_trades_once()

        resolve.assert_awaited_once_with("BTC-USDT")
        self.assertEqual(results, {"BTC-USDT": {"resolved_count": 1}})

    async def test_final_report_and_execution_trace_survive_memory_cleanup(self):
        await init_db()
        await save_analysis_history(
            "durable",
            "BTC-USDT",
            {"direction": "neutral", "confidence": 0.2, "key_findings": ["audit"]},
            [],
            execution_trace=[{"node": "synthesis", "observed_at": "now"}],
            execution_metrics={"duration_ms": 1234, "backend_trace_steps": 1},
        )
        record = await get_analysis_by_task_id("durable")
        self.assertEqual(record["report"]["key_findings"], ["audit"])
        self.assertEqual(record["execution_trace"][0]["node"], "synthesis")
        self.assertEqual(record["execution_metrics"]["duration_ms"], 1234)


if __name__ == "__main__":
    unittest.main()
