"""Hand-checked scorecard derivation from Cricsheet deliveries."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from backend.src.cricsheet_scorecard import derive_scorecard, dismissal_text, result_text, toss_text  # noqa: E402


def ball(batter, bowler, runs=0, extras=None, wicket=None, partner="B2", non_boundary=False):
    extra = extras or {}
    row = {"batter": batter, "bowler": bowler, "non_striker": partner,
           "runs": {"batter": runs, "extras": sum(extra.values()), "total": runs + sum(extra.values())}}
    if non_boundary:
        row["runs"]["non_boundary"] = True
    if extra:
        row["extras"] = extra
    if wicket:
        row["wickets"] = [wicket]
    return row


MATCH = {
    "info": {
        "teams": ["Chennai Super Kings", "Mumbai Indians"], "overs": 20,
        "toss": {"winner": "Mumbai Indians", "decision": "field"},
        "outcome": {"winner": "Chennai Super Kings", "by": {"runs": 3}},
        "player_of_match": ["B1"],
    },
    "innings": [
        {"team": "Chennai Super Kings", "overs": [
            {"over": 0, "deliveries": [  # X: maiden (byes are not charged)
                ball("B1", "X"), ball("B1", "X"), ball("B1", "X", extras={"byes": 2}),
                ball("B1", "X"), ball("B1", "X"), ball("B1", "X"),
            ]},
            {"over": 1, "deliveries": [
                ball("B1", "Y", 4),                                  # boundary
                ball("B1", "Y", extras={"wides": 1}),                # not a ball faced
                ball("B1", "Y", 1, extras={"noballs": 1}),           # faced, charged 2
                ball("B2", "Y", 4, non_boundary=True, partner="B1"), # all-run four
                ball("B2", "Y", extras={"legbyes": 1}, partner="B1"),
                ball("B1", "Y", 0, wicket={"player_out": "B2", "kind": "run out", "fielders": [{"name": "F1"}]}),
                ball("B1", "Y", 6, partner="B3"),
                ball("B1", "Y", 0, wicket={"player_out": "B1", "kind": "caught", "fielders": [{"name": "Y"}]}, partner="B3"),
            ]},
            {"over": 2, "deliveries": [
                ball("B3", "X", 0, wicket={"player_out": "B3", "kind": "retired hurt"}, partner="B4"),
            ]},
        ]},
        {"team": "Mumbai Indians", "overs": [{"over": 0, "deliveries": [ball("M1", "C1", 1, partner="M2")]}]},
    ],
}


class DerivedScorecardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.card = derive_scorecard(MATCH)
        cls.first = cls.card["content"]["innings"][0]
        cls.batters = {row["player"]["longName"]: row for row in cls.first["inningBatsmen"]}
        cls.bowlers = {row["player"]["longName"]: row for row in cls.first["inningBowlers"]}

    def test_innings_totals(self):
        # 2 byes + 4 + 1wd + (1+1nb) + 4 + 1lb + 6 = 20
        self.assertEqual(self.first["runs"], 20)
        self.assertEqual(self.first["wickets"], 2)  # retired hurt is not a wicket
        # 13 legal balls: the wide and no-ball are excluded, the retirement ball counts
        self.assertEqual(self.first["overs"], 2.1)
        self.assertEqual(self.first["extrasBreakdown"], {"wides": 1, "noballs": 1, "byes": 2, "legbyes": 1, "penalty": 0})
        self.assertEqual(self.first["extras"], 5)

    def test_batting(self):
        b1 = self.batters["B1"]
        self.assertEqual((b1["runs"], b1["balls"], b1["fours"], b1["sixes"]), (11, 11, 1, 1))
        self.assertEqual(b1["dismissalText"]["long"], "c & b Y")
        b2 = self.batters["B2"]
        self.assertEqual((b2["runs"], b2["balls"], b2["fours"]), (4, 2, 0))  # all-run four is not a boundary
        self.assertEqual(b2["dismissalText"]["long"], "run out (F1)")
        self.assertTrue(b2["isOut"])
        self.assertFalse(self.batters["B3"]["isOut"])  # retired hurt
        self.assertEqual(self.batters["B3"]["dismissalText"]["long"], "retired hurt")
        self.assertEqual([row["player"]["longName"] for row in self.first["inningBatsmen"]][:3], ["B1", "B2", "B3"])

    def test_bowling(self):
        x, y = self.bowlers["X"], self.bowlers["Y"]
        self.assertEqual((x["overs"], x["conceded"], x["maidens"], x["wickets"]), (1.1, 0, 1, 0))
        # charged: 4 + 1wd + 2(nb) + 4 + 0(lb) + 6 = 17; run out not credited
        self.assertEqual((y["overs"], y["conceded"], y["wickets"], y["maidens"]), (1.0, 17, 1, 0))
        self.assertEqual(y["economy"], 17.0)

    def test_fall_of_wickets_and_overs(self):
        self.assertEqual([(row["wicket"], row["runs"], row["player"]) for row in self.first["inningFallOfWickets"]], [(1, 14, "B2"), (2, 20, "B1")])
        self.assertEqual([row["overRuns"] for row in self.first["inningOvers"]], [2, 18, 0])
        second = self.card["content"]["innings"][1]
        self.assertEqual(second["target"], 21)
        self.assertEqual(second["inningOvers"][0]["requiredRuns"], 20)

    def test_match_text(self):
        self.assertEqual(result_text(MATCH["info"]), "CSK won by 3 runs")
        self.assertEqual(toss_text(MATCH["info"]), "MI won the toss and chose to field")
        self.assertEqual(result_text({"outcome": {"result": "tie", "eliminator": "Delhi Daredevils"}}), "Match tied (DC won the Super Over)")
        self.assertEqual(result_text({"outcome": {"winner": "Kings XI Punjab", "by": {"wickets": 1}, "method": "D/L"}}), "PBKS won by 1 wicket (D/L)")
        self.assertEqual(dismissal_text({"kind": "stumped", "fielders": [{"name": "Dhoni"}]}, "Jadeja"), "st Dhoni b Jadeja")


if __name__ == "__main__":
    unittest.main()
