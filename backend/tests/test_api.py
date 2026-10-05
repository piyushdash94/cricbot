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
        self.assertIn("extract_entities", [node["id"] for node in payload["nodes"]])
        self.assertIn("Understand request", [node["label"] for node in payload["nodes"]])
        self.assertTrue(any(edge.get("condition") for edge in payload["edges"]))
        self.assertIn("ui_context", payload["state_fields"])

    def test_ipl_seasons_and_validation_contract(self):
        payload = self.client.get("/api/ipl/seasons").json()
        self.assertEqual([item["year"] for item in payload["seasons"]], [2025, 2024, 2023])
        response = self.client.get("/api/ipl/matches", params={"season": 1999})
        self.assertEqual(response.status_code, 400)

    def test_docs_catalog_explains_contracts_and_graph(self):
        response = self.client.get("/api/docs/catalog")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(any(api["path"] == "/api/ipl/matches" for api in payload["apis"]))
        self.assertTrue(any(tool["owner"] == "extract_entities" for tool in payload["tools"]))
        self.assertTrue(any(tool["owner"] == "retrieve_facts" for tool in payload["tools"]))
        self.assertTrue(any(edge.get("condition") for edge in payload["transitions"]))
        levels = [item["level"] for item in payload["coverage"]]
        self.assertEqual(levels[:2], ["cricsheet", "full"])

    @patch.object(agent.llm, "chat", return_value=("RCB's biggest swing came late, and the momentum view shows it.", "groq", "llama-3.3-70b-versatile"))
    def test_agent_loads_the_match_and_summarises_with_llm(self, chat):
        response = self.client.post("/api/agent/chat", json={
            "message": "Show me the momentum for RCB vs PBKS",
            "history": [],
            "ui_context": {},
        })
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["model_used"])
        self.assertEqual(payload["provider"], "groq")
        chat.assert_called_once()
        facts = chat.call_args.args[0][-1]["content"]
        self.assertIn("Result: RCB won by 6 runs", facts)
        self.assertIn("Batting:", facts)
        self.assertEqual(payload["cards"][0]["type"], "match")
        self.assertEqual(payload["cards"][0]["match"]["title"], "RCB vs PBKS")
        self.assertIn({"type": "set_match_tab", "tab": "analytics"}, payload["ui_actions"])
        self.assertTrue(any(action["type"] == "open_match" for action in payload["ui_actions"]))
        visited = [event["node"] for event in payload["trace"]]
        self.assertEqual(visited[:2], ["normalize_request", "extract_entities"])
        self.assertIn("load_match", visited)
        self.assertIn("llm_response", visited)
        self.assertEqual(visited[-1], "finalize")

    @patch.object(agent.llm, "chat", return_value=("RCB won by 60 runs thanks to 99 from Kohli.", "openrouter", "test-model"))
    def test_invented_numbers_fall_back_to_grounded_recap(self, _chat):
        payload = self.client.post("/api/agent/chat", json={"message": "Tell me about RCB vs PBKS", "history": [], "ui_context": {}}).json()
        self.assertFalse(payload["model_used"])
        self.assertEqual(payload["validation"], "fallback-after-validation")
        self.assertIn("RCB won by 6 runs", payload["reply"])
        self.assertNotIn("99", payload["reply"])

    @patch.object(agent.llm, "chat", side_effect=RuntimeError("groq: offline; openrouter: offline"))
    def test_offline_llm_still_answers_from_facts(self, _chat):
        payload = self.client.post("/api/agent/chat", json={"message": "Tell me about RCB vs PBKS", "history": [], "ui_context": {}}).json()
        self.assertFalse(payload["model_used"])
        self.assertIn("RCB won by 6 runs", payload["reply"])
        self.assertEqual(payload["cards"][0]["type"], "match")

    def test_offline_momentum_question_leads_with_the_decisive_shift(self):
        from backend.agent.match_facts import deterministic_summary
        card = {
            "match": {"title": "RCB vs PBKS", "status_text": "RCB won by 6 runs", "season": 2025, "label": "Final"},
            "innings": [], "player_of_match": [],
            "momentum_shifts": [
                {"type": "wicket_cluster", "innings_team": "PBKS", "from": "16.1", "to": "17.4", "headline": "3 wickets for 9 runs in 10 balls",
                 "score_before": "150/4", "score_after": "159/7", "favours": "RCB", "magnitude": 30.0, "decisive": True},
                {"type": "scoring_burst", "innings_team": "PBKS", "from": "5.1", "to": "6.6", "headline": "34 runs in 2 overs with 6 boundaries",
                 "score_before": "40/1", "score_after": "74/1", "favours": "PBKS", "magnitude": 14.0},
            ],
        }
        reply = deterministic_summary(card, "Where did this match turn?")
        self.assertIn("decisive shift was a wicket cluster in the PBKS innings, overs 16.1–17.4", reply)
        self.assertIn("scoring burst in overs 5.1–6.6", reply)
        self.assertNotIn("decisive", deterministic_summary(card, "Give me the scorecard"))

    @patch.object(agent.llm, "chat", return_value=("RCB won by 6 runs.", "groq", "m"))
    def test_streaming_agent_emits_trace_before_result(self, _chat):
        response = self.client.post("/api/agent/chat/stream", json={
            "message": "Tell me about RCB vs PBKS",
            "history": [],
            "ui_context": {},
        })
        self.assertEqual(response.status_code, 200)
        events = [__import__("json").loads(line) for line in response.text.splitlines()]
        self.assertEqual(events[0]["event"], "trace")
        self.assertEqual(events[-1]["event"], "result")
        self.assertIn("llm_response", [item["data"].get("node") for item in events[:-1]])


if __name__ == "__main__":
    unittest.main()
