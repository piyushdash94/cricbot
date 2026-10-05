"""Offline tests for the ball-by-ball source ladder (Cricsheet, ESPN, fallbacks).

Payloads are synthetic but follow the published Cricsheet JSON format and the
ESPN play-by-play item shape used by cricdata.
"""

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from backend.src import cricsheet, ipl_data  # noqa: E402


def delivery(batter, bowler, runs=0, extras=None, wicket=None, non_boundary=False):
    extra_total = sum((extras or {}).values())
    row = {
        "batter": batter, "bowler": bowler, "non_striker": "Partner",
        "runs": {"batter": runs, "extras": extra_total, "total": runs + extra_total},
    }
    if non_boundary:
        row["runs"]["non_boundary"] = True
    if extras:
        row["extras"] = extras
    if wicket:
        row["wickets"] = [wicket]
    return row


def cricsheet_match(teams=("Royal Challengers Bangalore", "Kings XI Punjab"), day="2025-06-03"):
    return {
        "meta": {"data_version": "1.1.0"},
        "info": {"dates": [day], "teams": list(teams), "event": {"name": "Indian Premier League", "stage": "Final"}, "venue": "Narendra Modi Stadium"},
        "innings": [
            {"team": teams[0], "overs": [
                {"over": 0, "deliveries": [
                    delivery("Salt", "Arshdeep", 4),
                    delivery("Salt", "Arshdeep", extras={"wides": 1}),
                    delivery("Salt", "Arshdeep", 0, wicket={"player_out": "Salt", "kind": "caught", "fielders": [{"name": "Iyer"}]}),
                    delivery("Kohli", "Arshdeep", 4, non_boundary=True),
                    delivery("Kohli", "Arshdeep", extras={"legbyes": 1}),
                    delivery("Patidar", "Arshdeep", 6),
                    delivery("Patidar", "Arshdeep", 1),
                ]},
            ]},
            {"team": teams[1], "overs": [{"over": 0, "deliveries": [delivery("Arya", "Hazlewood", 1)]}]},
            {"team": teams[1], "super_over": True, "overs": [{"over": 0, "deliveries": [delivery("Arya", "Hazlewood", 6)]}]},
        ],
    }


class CricsheetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "1473511.json").write_text(json.dumps(cricsheet_match()))
        self.env = patch.dict(os.environ, {"CRICSHEET_DIR": str(self.dir)})
        self.env.start()
        cricsheet._index.cache_clear()

    def tearDown(self):
        self.env.stop()
        cricsheet._index.cache_clear()
        self.tmp.cleanup()

    def test_matches_renamed_franchises_across_utc_date_shift(self):
        found = cricsheet.find_match("2025-06-02T14:00:00Z", ["Royal Challengers Bengaluru", "Punjab Kings"])
        self.assertEqual(found.name, "1473511.json")
        self.assertIsNone(cricsheet.find_match("2025-06-10", ["Royal Challengers Bengaluru", "Punjab Kings"]))
        self.assertIsNone(cricsheet.find_match("2025-06-03", ["Mumbai Indians", "Punjab Kings"]))

    def test_normalizes_every_delivery_with_scorecard_conventions(self):
        balls = cricsheet.load_deliveries(self.dir / "1473511.json")["balls"]
        self.assertEqual(len(balls), 8)  # super over excluded
        labels = [ball["label"] for ball in balls[:7]]
        self.assertEqual(labels, ["0.1", "0.2", "0.2", "0.3", "0.4", "0.5", "0.6"])
        wide, wicket, run_four, legbye, six = balls[1], balls[2], balls[3], balls[4], balls[5]
        self.assertEqual(wide["event"], "1wd")
        self.assertEqual(wicket["event"], "W")
        self.assertIn("caught (Iyer)", wicket["text"])
        self.assertEqual(run_four["event"], "4")
        self.assertFalse(run_four["boundary"])  # four runs, not a boundary
        self.assertEqual(legbye["event"], "1lb")
        self.assertTrue(six["boundary"])
        self.assertEqual(balls[6]["score"], "17/1")
        self.assertEqual(balls[7]["inning"], 2)

    def test_index_is_built_on_demand(self):
        self.assertFalse((self.dir / cricsheet.INDEX_FILE).exists())
        cricsheet.find_match("2025-06-03", ["Royal Challengers Bengaluru", "Punjab Kings"])
        self.assertTrue((self.dir / cricsheet.INDEX_FILE).exists())

    def test_sync_extracts_archive_and_indexes(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("1000.json", json.dumps(cricsheet_match(("Chennai Super Kings", "Mumbai Indians"), "2023-04-08")))
            archive.writestr("README.txt", "ignored")
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=buffer.getvalue())))
        target = self.dir / "synced"
        self.assertEqual(cricsheet.sync(target, client=client), 1)
        self.assertTrue((target / "1000.json").exists())
        self.assertFalse((target / "README.txt").exists())

    def test_match_detail_prefers_cricsheet_and_skips_live_feeds(self):
        class Offline:
            calls = []

            def __getattr__(self, name):
                def call(*_args):
                    Offline.calls.append(name)
                    raise RuntimeError("offline")
                return call

        with patch.object(ipl_data, "_client", return_value=Offline()):
            detail = ipl_data.get_match_detail(ipl_data.IPL_SERIES[2025]["slug"], ipl_data.DEMO_MATCH_SLUG)
        self.assertEqual(detail["ball_coverage"]["level"], "cricsheet")
        self.assertEqual(len(detail["balls"]), 8)
        self.assertIn("Cricsheet", detail["sources"][2]["provider"])
        self.assertNotIn("match_ball_by_ball", Offline.calls)
        self.assertNotIn("match_commentary", Offline.calls)


class EspnPlayByPlayTests(unittest.TestCase):
    def item(self, actual, play, score, *, wicket=False, number=1):
        return {
            "id": f"{number}-{actual}", "playType": {"description": play}, "scoreValue": score,
            "over": {"actual": actual}, "innings": {"number": number},
            "bowler": {"athlete": {"displayName": "Hazlewood"}}, "batsman": {"athlete": {"displayName": "Iyer"}, "runs": score},
            "dismissal": {"dismissal": wicket}, "text": "Commentary line", "shortText": "Hazlewood to Iyer",
        }

    def test_normalizes_grouped_ball_items(self):
        payload = [[self.item(0.2, "four", 4), self.item(0.1, "no run", 0)], [self.item(0.1, "out", 0, wicket=True, number=2), self.item(0.1, "wide", 1, number=2)]]
        balls = ipl_data._normalize_espn_balls(payload)
        self.assertEqual([ball["label"] for ball in balls], ["0.1", "0.2", "0.1", "0.1"])
        self.assertTrue(balls[1]["boundary"])
        self.assertEqual({ball["event"] for ball in balls[2:]}, {"W", "1wd"})
        self.assertEqual(balls[0]["title"], "Hazlewood to Iyer")

    def test_rejects_non_list_payloads(self):
        self.assertEqual(ipl_data._normalize_espn_balls({"comments": []}), [])

    def test_espn_feed_is_used_when_cricsheet_has_no_match(self):
        class Client:
            def match_ball_by_ball(self, *_args):
                return [[EspnPlayByPlayTests.item(None, 0.1, "four", 4)]]

            def __getattr__(self, _name):
                def missing(*_args):
                    raise RuntimeError("offline")
                return missing

        with tempfile.TemporaryDirectory() as empty, patch.dict(os.environ, {"CRICSHEET_DIR": empty}), patch.object(ipl_data, "_client", return_value=Client()):
            cricsheet._index.cache_clear()
            detail = ipl_data.get_match_detail(ipl_data.IPL_SERIES[2025]["slug"], ipl_data.DEMO_MATCH_SLUG)
        cricsheet._index.cache_clear()
        self.assertEqual(detail["ball_coverage"]["level"], "full")
        self.assertEqual(detail["sources"][2]["provider"], "ESPN play-by-play")


if __name__ == "__main__":
    unittest.main()
