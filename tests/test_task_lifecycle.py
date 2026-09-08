import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from backend import main


class TaskLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main.task_events.clear()
        main.task_signals.clear()
        main.task_done.clear()
        main.task_event_seq.clear()
        main.task_handles.clear()

    async def asyncTearDown(self):
        for task in list(main.task_handles.values()) + list(main.cleanup_handles):
            task.cancel()
        await asyncio.gather(
            *list(main.task_handles.values()),
            *list(main.cleanup_handles),
            return_exceptions=True,
        )
        main.task_handles.clear()
        main.cleanup_handles.clear()

    async def test_concurrency_limit_and_cancel(self):
        started = asyncio.Event()
        blocker = asyncio.Event()

        async def fake_analysis(*_args):
            started.set()
            await blocker.wait()

        with (
            patch.object(main.config, "MAX_CONCURRENT_ANALYSES", 1),
            patch("backend.main._run_analysis", side_effect=fake_analysis),
        ):
            response = await main.start_analysis(main.AnalyzeRequest())
            await started.wait()
            with self.assertRaises(HTTPException) as raised:
                await main.start_analysis(main.AnalyzeRequest())
            self.assertEqual(raised.exception.status_code, 429)

            cancelled = await main.cancel_analysis(response.task_id)
            self.assertEqual(cancelled["status"], "cancelled")

    async def test_graph_state_is_emitted_before_graph_finishes(self):
        release = asyncio.Event()

        class FakeGraph:
            async def astream(self, initial_state, stream_mode):
                state = dict(initial_state)
                state["trace"] = [{
                    "node": "orchestrator",
                    "decision": {"action": "call_agents"},
                }]
                yield state
                await release.wait()
                state = dict(state)
                state["synthesis_result"] = {
                    "direction": "neutral",
                    "confidence": 0.5,
                }
                yield state

        task_id = "stream-test"
        main._ensure_task(task_id)
        with (
            patch("backend.main.get_analysis_graph", return_value=FakeGraph()),
            patch("backend.main.save_analysis_history", AsyncMock()),
            patch("backend.main._create_backtest_track", AsyncMock()),
        ):
            task = asyncio.create_task(main._run_analysis(task_id, "BTC-USDT", "test"))
            for _ in range(50):
                if any(
                    event.get("data", {}).get("agent") == "orchestrator"
                    for event in main.task_events[task_id]
                ):
                    break
                await asyncio.sleep(0.01)

            self.assertFalse(task.done())
            self.assertTrue(any(
                event.get("data", {}).get("agent") == "orchestrator"
                for event in main.task_events[task_id]
            ))
            release.set()
            await task

        sequences = [event["seq"] for event in main.task_events[task_id]]
        self.assertEqual(sequences, sorted(set(sequences)))

    def test_stale_news_caps_confidence_and_forces_observe(self):
        result = {"direction": "bearish", "confidence": 0.72, "position_advice": {}}
        guarded = main._apply_execution_freshness_guard(
            result,
            {"news_freshness_status": "stale_at_completion"},
        )
        self.assertEqual(guarded["confidence"], 0.45)
        self.assertEqual(guarded["position_advice"]["action"], "观望（自动）")


if __name__ == "__main__":
    unittest.main()
