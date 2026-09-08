import asyncio
import unittest
from types import SimpleNamespace

from backend.llm.hy3_client import UnifiedClient


def make_client(async_api):
    client = UnifiedClient.__new__(UnifiedClient)
    client._async_client = async_api
    client._fast = "fast"
    client._slow = "slow"
    client._supports_effort = False
    client._fallback_ready = False
    client._failover_until = 0.0
    client._primary_label = "test"
    client._fallback_label = "fallback"
    client._active_label = "test"
    return client


class LlmCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_reaches_active_http_request(self):
        started = asyncio.Event()
        blocker = asyncio.Event()

        class Completions:
            async def create(self, **_kwargs):
                started.set()
                await blocker.wait()

        api = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        client = make_client(api)
        task = asyncio.create_task(client.achat(messages=[]))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_async_function_calling_awaits_tools(self):
        tool_call = SimpleNamespace(
            id="call-1",
            function=SimpleNamespace(name="lookup", arguments='{"value": 7}'),
        )
        first = SimpleNamespace(choices=[SimpleNamespace(
            reasoning_content=None,
            message=SimpleNamespace(content="", reasoning_content=None, tool_calls=[tool_call]),
        )])
        second = SimpleNamespace(choices=[SimpleNamespace(
            reasoning_content=None,
            message=SimpleNamespace(content="done", reasoning_content=None, tool_calls=[]),
        )])

        class Completions:
            def __init__(self):
                self.responses = [first, second]

            async def create(self, **_kwargs):
                return self.responses.pop(0)

        async def lookup(value):
            return {"value": value}

        api = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        client = make_client(api)
        result = await client.afunction_calling_loop(
            messages=[],
            tools=[{"type": "function"}],
            tool_map={"lookup": lookup},
        )

        self.assertEqual(result["final_message"], "done")
        self.assertEqual(result["tool_calls_log"][0]["result"], {"value": 7})


if __name__ == "__main__":
    unittest.main()
