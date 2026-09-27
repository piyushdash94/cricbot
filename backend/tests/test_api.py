"""HTTP contract tests for the FastAPI layer."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi.testclient import TestClient  # noqa: E402

from backend.apis.demo_data import demo_scorecard  # noqa: E402
from backend.apis.main import app  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
