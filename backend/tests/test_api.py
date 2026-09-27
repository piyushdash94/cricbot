"""HTTP contract tests for the FastAPI layer."""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi.testclient import TestClient  # noqa: E402

from backend.apis.demo_data import demo_scorecard  # noqa: E402
from backend.apis.main import agent, app  # noqa: E402


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_health(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_dashboard_contains_ui_features(self):
        payload = self.client.get("/api/dashboard").json()
        self.assertIn("featured_match", payload)
        self.assertIn("standings", payload)
        self.assertIn("analytics", payload)
        self.assertIn("impact", payload["analytics"])
        self.assertIn("pitch", payload["analytics"])
        self.assertIn("summary", payload)

    def test_render_scorecard(self):
        response = self.client.post(
            "/api/scorecard/render",
            json={"info": {}, "scorecard": demo_scorecard(), "options": {"max_chars": 1200}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("RCB vs PBKS", response.json()["text"])
        self.assertLessEqual(response.json()["characters"], 1200)

    def test_analytics_contract(self):
        response = self.client.post("/api/analytics", json={"scorecard": demo_scorecard()})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["momentum"]), 2)
        self.assertGreater(len(payload["turning_points"]), 0)

    def test_standings_view(self):
        standings = {"groups": [{"teamStats": [{
            "rank": 1,
            "teamInfo": {"abbreviation": "RCB"},
            "matchesPlayed": 14,
            "matchesWon": 9,
            "matchesLost": 4,
            "points": 19,
            "nrr": 0.301,
        }]}]}
        response = self.client.post("/api/views/standings", json={"payload": standings})
        self.assertIn("RCB", response.json()["text"])

    def test_search_routes_to_multiple_tools(self):
        response = self.client.get("/api/search", params={"q": "RCB impact"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("search_matches", payload["sources"])
        self.assertIn("search_analytics", payload["sources"])
        self.assertGreater(len(payload["results"]), 0)

    def test_agent_graph_exposes_nodes_and_conditional_edges(self):
        response = self.client.get("/api/agent/graph")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["framework"], "LangGraph")
        self.assertIn("route_tools", [node["id"] for node in payload["nodes"]])
        self.assertIn("Understand request", [node["label"] for node in payload["nodes"]])
        self.assertTrue(any(edge.get("condition") for edge in payload["edges"]))

    @patch.object(agent.gemma, "complete", return_value="RCB's biggest swing came late, and I opened the momentum view.")
    def test_agent_returns_grounded_actions(self, complete):
        response = self.client.post("/api/agent/chat", json={
            "message": "Show me the momentum for RCB vs PBKS",
            "history": [],
            "ui_context": {"selected_match": "rcb-pbks"},
        })
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["model_used"])
        complete.assert_not_called()
        self.assertIn("Biggest momentum swing", payload["reply"])
        self.assertIn("search_matches", [call["name"] for call in payload["tool_calls"]])
        self.assertIn({"type": "set_view", "view": "momentum"}, payload["ui_actions"])
        visited = [event["node"] for event in payload["trace"]]
        self.assertEqual(visited[0], "normalize_request")
        self.assertIn("exact_response", visited)
        self.assertNotIn("gemma_response", visited)
        self.assertEqual(visited[-1], "finalize")

    @patch.object(agent.gemma, "complete", return_value="RCB won by 6 runs.")
    def test_streaming_agent_emits_trace_before_result(self, _complete):
        response = self.client.post("/api/agent/chat/stream", json={
            "message": "Tell me about RCB vs PBKS",
            "history": [],
            "ui_context": {"selected_match": "rcb-pbks"},
        })
        self.assertEqual(response.status_code, 200)
        events = [__import__("json").loads(line) for line in response.text.splitlines()]
        self.assertEqual(events[0]["event"], "trace")
        self.assertEqual(events[-1]["event"], "result")
        self.assertIn("gemma_response", [item["data"].get("node") for item in events[:-1]])



if __name__ == "__main__":
    unittest.main()
