import unittest
from unittest.mock import patch

from backend.agents import graph as graph_module


class GraphExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_timed_wrappers_execute_and_record_timings(self):
        async def orchestrator(state):
            if not state.get("evidence_pool"):
                agents = ["technical", "onchain", "derivatives", "sentiment", "macro"]
                action = "call_agents"
            else:
                agents = []
                action = "synthesize"
            state["orchestrator_decision"] = {"action": action, "agents": agents}
            state["step"] = state.get("step", 0) + 1
            state.setdefault("trace", []).append({"node": "orchestrator"})
            return state

        async def synthesis(state):
            state["synthesis_result"] = {"direction": "neutral", "confidence": 0.4}
            state.setdefault("trace", []).append({"node": "synthesis"})
            return state

        async def critic(state):
            state.setdefault("trace", []).append({"node": "critic"})
            return state

        def make_agent(name):
            async def agent(state):
                state.setdefault("evidence_pool", []).append({
                    "agent": name,
                    "bias": "neutral",
                    "score": 50,
                    "confidence": 0.8,
                    "data_quality": "real",
                })
                state.setdefault("trace", []).append({"node": name})
                return state

            return agent

        agents = {name: make_agent(name) for name in graph_module.AGENT_MAP}
        with (
            patch.object(graph_module, "run_orchestrator", orchestrator),
            patch.object(graph_module, "run_synthesis", synthesis),
            patch.object(graph_module, "run_critic", critic),
            patch.object(graph_module, "AGENT_MAP", agents),
            patch.object(graph_module, "_should_continue_critic_loop", lambda _: "finalize"),
        ):
            graph = graph_module.build_graph()
            initial = {
                "query": "graph smoke test",
                "symbol": "BTC-USDT",
                "evidence_pool": [],
                "step": 0,
                "trace": [],
                "_execution_timings": [],
                "critic_round": 0,
                "critic_feedback": [],
                "synthesis_result": None,
            }
            final = None
            async for state in graph.astream(initial, stream_mode="values"):
                final = state

        self.assertEqual(len(final["evidence_pool"]), 5)
        self.assertEqual(final["synthesis_result"]["direction"], "neutral")
        timing_nodes = {entry["node"] for entry in final["_execution_timings"]}
        self.assertTrue({"orchestrator", "run_agents", "synthesis", "critic"}.issubset(timing_nodes))
        self.assertTrue(all(entry["duration_ms"] >= 0 for entry in final["_execution_timings"]))


if __name__ == "__main__":
    unittest.main()
