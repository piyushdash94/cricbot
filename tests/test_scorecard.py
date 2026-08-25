"""
Tests for the scorecard toolkit.

Everything here runs offline against synthetic payloads shaped like the real
ESPNCricinfo responses, so no network access is needed.

    pytest            # or: python tests/test_scorecard.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scorecard_helpers import (  # noqa: E402
    get_nested, player_name, short_name, valid_batter, valid_bowler,
    get_bowler_runs, dismissal_text, extract_fow, innings_team_name,
    over_series, phase_splits, partnership_rows, win_prob_series,
    cumulative_runs, player_of_match,
)
from scorecard_charts import (  # noqa: E402
    sparkline, hbar, manhattan, win_probability, worm, partnership_bars,
    phase_table,
)
from scorecard_renderer import (  # noqa: E402
    build_compact_scorecard, format_number, truncate_at_limit,
)
from cricbot_views import (  # noqa: E402
    format_live_matches, format_standings, format_player_card,
    format_match_preview, match_slugs,
)
from impact_lab import (  # noqa: E402
    match_par, batting_impact, bowling_impact, impact_leaderboard,
    turning_points, momentum_series, pitch_profile, bowler_kind,
)


# ---------------------------------------------------------------- fixtures


def make_innings(team="RCB", runs=190, wickets=9):
    """An innings shaped like content.innings[i] from a real scorecard."""
    return {
        "team": {"abbreviation": team},
        "runs": runs, "wickets": wickets, "overs": 20.0, "balls": 120,
        "extras": 8, "fours": 14, "sixes": 7,
        "inningBatsmen": [
            {"battedType": "yes", "runs": 61, "balls": 30, "fours": 5,
             "sixes": 4, "strikerate": 203.3, "isOut": True, "fowRuns": 95,
             "player": {"longName": "Phil Salt", "name": "PD Salt",
                        "fieldingName": "Salt"}},
            {"battedType": "yes", "runs": 43, "balls": 35, "fours": 4,
             "sixes": 1, "strikerate": 122.8, "isOut": False, "fowRuns": 0,
             "player": {"longName": "Virat Kohli", "name": "V Kohli",
                        "fieldingName": "Kohli"}},
            # A substitute must never appear in output.
            {"battedType": "sub", "runs": 99, "balls": 1,
             "player": {"longName": "Should Not Appear"}},
        ],
        # Wickets taken here must equal the batters marked isOut above, or
        # the impact zero-sum invariant cannot hold. Real matches balance
        # this way apart from run-outs.
        "inningBowlers": [
            {"overs": 4.0, "balls": 24, "wickets": 1, "conceded": 26,
             "economy": 6.5, "dots": 8,
             "player": {"longName": "Krunal Pandya", "name": "KH Pandya",
                        "fieldingName": "Pandya", "bowlingStyles": ["sla"]}},
            # No 'conceded' — must fall back to overs x economy.
            {"overs": 4.0, "balls": 24, "wickets": 0, "economy": 9.0,
             "player": {"longName": "Economy Only",
                        "bowlingStyles": ["rfm"]}},
        ],
        "inningFallOfWickets": [
            {"fowRuns": 95, "fowWicketNum": 1, "fowOvers": 8.3},
        ],
        "inningWickets": [
            {"dismissalText": {"long": "c Iyer b Jamieson"},
             "player": {"longName": "Phil Salt"}},
        ],
        "inningPartnerships": [
            {"runs": 95, "balls": 51, "overs": 8.3,
             "player1Runs": 40, "player1Balls": 25,
             "player2Runs": 55, "player2Balls": 26,
             "player1": {"longName": "Virat Kohli", "fieldingName": "Kohli"},
             "player2": {"longName": "Phil Salt", "fieldingName": "Salt"}},
        ],
        "inningOverGroups": [
            {"type": "POWERPLAY", "startOverNumber": 1, "endOverNumber": 6,
             "oversRuns": 60, "oversWickets": 1},
            {"type": "FINAL_OVERS", "startOverNumber": 17,
             "endOverNumber": 20, "oversRuns": 40, "oversWickets": 3},
        ],
        "inningOvers": [
            {"overNumber": n, "overRuns": 8 + n % 5, "overWickets": n % 4 == 0,
             "totalRuns": 10 * n, "totalWickets": n // 4,
             "predictions": {"winProbability": 40.0 + n * 2, "score": 190}}
            for n in range(1, 6)
        ],
    }


def make_scorecard(n_innings=2):
    teams = ["RCB", "PBKS"]
    return {
        "match": {
            "statusText": "PBKS won by 6 runs",
            "ground": {"name": "Narendra Modi Stadium"},
            "teams": [{"team": {"abbreviation": t}} for t in teams],
        },
        "content": {
            "innings": [make_innings(teams[i]) for i in range(n_innings)],
            "supportInfo": {
                "playersOfTheMatch": [
                    {"player": {"longName": "Krunal Pandya"}}
                ]
            },
        },
    }


INFO = {"statusText": "PBKS won by 6 runs",
        "ground": {"name": "Narendra Modi Stadium"}}


# ----------------------------------------------------------------- helpers


def test_get_nested():
    assert get_nested({"a": {"b": {"c": 1}}}, ["a", "b", "c"]) == 1
    assert get_nested({"a": {}}, ["a", "x"]) == "-"
    assert get_nested(None, ["a"]) == "-"


def test_player_name_shapes():
    assert player_name({"player": {"longName": "Virat Kohli"}}) == "Virat Kohli"
    assert player_name({"athlete": {"displayName": "V Kohli"}}) == "V Kohli"
    assert player_name({}) == "Unknown"


def test_short_name_prefers_fielding_name():
    assert short_name({"fieldingName": "Narine"}) == "Narine"
    assert short_name({"name": "Ramandeep Singh"}) == "Singh"
    assert short_name({}) == "?"


def test_batter_and_bowler_validation():
    assert valid_batter({"battedType": "yes", "runs": 10})
    assert not valid_batter({"battedType": "sub", "runs": 10})
    assert not valid_batter({"battedType": "yes", "runs": None})
    assert valid_bowler({"overs": 4.0})
    assert not valid_bowler({"overs": None})


def test_bowler_runs_falls_back_to_economy():
    assert get_bowler_runs({"conceded": 26}) == 26
    assert get_bowler_runs({"overs": 4.0, "economy": 7.0}) == 28
    assert get_bowler_runs({}) == "-"


def test_dismissal_text_accepts_dict_or_string():
    assert dismissal_text({"long": "c A b B"}) == "c A b B"
    assert dismissal_text({"short": "caught"}) == "caught"
    assert dismissal_text("run out") == "run out"
    assert dismissal_text(None) == ""


def test_extract_fow_merges_wicket_detail():
    rows = extract_fow(make_innings())
    assert len(rows) == 1
    assert rows[0]["score"] == 95
    assert rows[0]["dismissal"] == "c Iyer b Jamieson"


def test_innings_analytics():
    inn = make_innings()
    assert innings_team_name(inn) == "RCB"
    assert innings_team_name({}) == "Unknown Team"
    assert len(over_series(inn)) == 5
    assert [p["name"] for p in phase_splits(inn)] == ["Powerplay", "Death"]
    assert partnership_rows(inn)[0]["player1_short"] == "Kohli"
    assert cumulative_runs(inn) == [10, 20, 30, 40, 50]


def test_win_prob_inverts_for_bowling_team():
    """Cricinfo reports win% for whoever is batting, so the tracked team's
    curve must be inverted in innings where they bowled."""
    innings = make_scorecard()["content"]["innings"]
    batting = win_prob_series(innings, "RCB")
    n = len(over_series(innings[0]))
    # First innings: RCB batting, values pass through.
    assert batting[0] == 42.0
    # Second innings: PBKS batting, RCB's chance is the complement.
    assert batting[n] == 100.0 - 42.0


def test_player_of_match():
    assert player_of_match(make_scorecard()) == "Krunal Pandya"
    assert player_of_match({"content": {}}) is None


# ------------------------------------------------------------------ charts


def test_sparkline_scaling():
    assert sparkline([]) == ""
    assert sparkline([5, 5, 5]) == "▅▅▅"          # flat, no divide-by-zero
    assert len(sparkline([1, 2, 3, 4])) == 4
    assert sparkline([0, 100], lo=0, hi=100) == "▁█"


def test_hbar_always_fixed_width():
    assert len(hbar(5, 10, width=10)) == 10
    assert len(hbar(1, 0, width=8)) == 8           # zero max must not crash
    assert len(hbar(None, 10, width=6)) == 6


def test_charts_return_placeholders_not_exceptions():
    assert manhattan([]) == "(no over data)"
    assert "no win probability" in win_probability([])
    assert "no partnership" in partnership_bars([])
    assert "no phase" in phase_table([])
    assert worm([]) == "(no data)"


def test_partnership_labels_stay_aligned():
    """Long names must not push the bars out of column."""
    rows = partnership_rows(make_innings())
    rows[0]["player1_short"] = "Averyverylongsurname"
    line = partnership_bars(rows, label_width=16)
    assert line.index("█") == 16


# ---------------------------------------------------------------- renderer


def test_format_number():
    assert format_number(None) == "-"
    assert format_number(0) == "0"


def test_default_scorecard_contents():
    out = build_compact_scorecard(INFO, make_scorecard())
    assert "RCB vs PBKS" in out
    assert "Phil Salt: 61(30)" in out
    assert "Krunal Pandya: 1/26" in out
    assert "Economy Only: 0/36" in out       # economy fallback
    assert "Should Not Appear" not in out    # substitute excluded
    assert "{" not in out                    # no raw dicts


def test_optional_sections():
    out = build_compact_scorecard(INFO, make_scorecard(), phases=True,
                                  charts=True, partnerships=True)
    assert "Phases:" in out
    assert "Runs/over" in out
    assert "Top stands:" in out
    assert "Win%" in out


def test_truncation_never_leaves_open_fence():
    sc = make_scorecard()
    for limit in (120, 200, 400, 800, 1500):
        out = build_compact_scorecard(INFO, sc, phases=True, charts=True,
                                      partnerships=True, max_chars=limit)
        assert len(out) <= limit, f"exceeded {limit}"
        assert out.count("```") % 2 == 0, f"open fence at {limit}"


def test_truncate_at_limit_closes_fence():
    text = "a\n```\n" + "x" * 500
    out = truncate_at_limit(text, 50)
    assert out.count("```") % 2 == 0


def test_empty_match_does_not_crash():
    out = build_compact_scorecard({}, {"content": {"innings": []}},
                                  phases=True, charts=True, partnerships=True)
    assert "Result unavailable" in out


# ------------------------------------------------------------------- views


def test_standings_uses_teaminfo_and_nrr():
    """Real payloads key these as teamInfo/nrr, not team/netRunRate."""
    standings = {"groups": [{"name": "", "teamStats": [
        {"teamInfo": {"abbreviation": "PBKS"}, "matchesPlayed": "14",
         "matchesWon": 9, "matchesLost": 4, "points": 19, "nrr": 0.372,
         "rank": 1},
        {"teamInfo": {"abbreviation": "CSK"}, "matchesPlayed": "14",
         "matchesWon": 4, "matchesLost": 10, "points": 8, "nrr": -0.647,
         "rank": 2},
    ]}]}
    out = format_standings(standings)
    assert "PBKS" in out and "+0.372" in out
    assert "-0.647" in out
    assert out.index("PBKS") < out.index("CSK")     # sorted by rank


def test_views_handle_empty_input():
    assert "No live matches" in format_live_matches([])
    assert "No standings" in format_standings({})
    assert "not found" in format_player_card(None)


def test_match_slugs():
    m = {"slug": "kkr-vs-rcb", "objectId": 1473442,
         "series": {"slug": "ipl-2025", "objectId": 1449924}}
    assert match_slugs(m) == ("ipl-2025-1449924", "kkr-vs-rcb-1473442")


def test_match_preview():
    out = format_match_preview({
        "venue": {"name": "Eden Gardens"},
        "toss": {"winner_team": "RCB", "decision": "field"},
        "captains": [{"name": "Rajat Patidar", "team_name": "RCB"}],
    })
    assert "Eden Gardens" in out
    assert "RCB chose to field" in out
    assert "Rajat Patidar" in out


# ------------------------------------------------------------------ impact


def test_match_par_from_match_itself():
    par = match_par(make_scorecard()["content"]["innings"])
    assert par["balls"] == 240
    assert par["par_rpb"] == 380 / 240


def test_impact_is_zero_sum_across_wicket_values():
    """wicket_runs is charged to batters and credited to bowlers, so the
    residual must not move as the parameter changes."""
    innings = make_scorecard()["content"]["innings"]
    par = match_par(innings)

    residuals = []
    for w in (0, 6, 12, 24):
        bat = sum(r["impact"] for r in batting_impact(innings, par, w))
        bowl = sum(r["impact"] for r in bowling_impact(innings, par, w))
        residuals.append(round(bat + bowl, 1))

    assert len(set(residuals)) == 1, f"residual drifted: {residuals}"


def test_impact_leaderboard_merges_roles():
    innings = make_scorecard()["content"]["innings"]
    board = impact_leaderboard(innings, match_par(innings))
    assert {r["role"] for r in board} == {"bat", "bowl"}
    impacts = [r["impact"] for r in board]
    assert impacts == sorted(impacts, reverse=True)


def test_turning_points_ranked_by_absolute_swing():
    innings = make_scorecard()["content"]["innings"]
    points = turning_points(innings, top_n=3)
    swings = [abs(p["swing"]) for p in points]
    assert swings == sorted(swings, reverse=True)


def test_momentum_series_length_matches_overs():
    inn = make_innings()
    assert len(momentum_series(inn)) == len(over_series(inn))
    assert momentum_series({}) == []


def test_bowler_kind_classification():
    assert bowler_kind({"bowlingStyles": ["sla"]}) == "spin"
    assert bowler_kind({"bowlingStyles": ["lbg"]}) == "spin"
    assert bowler_kind({"bowlingStyles": ["rfm"]}) == "pace"
    assert bowler_kind({"bowlingStyles": []}) == "unknown"


def test_pitch_profile_splits_pace_and_spin():
    profile = pitch_profile(make_scorecard()["content"]["innings"])
    assert profile["pace"]["balls"] > 0
    assert profile["spin"]["balls"] > 0
    assert profile["spin"]["wkts"] == 2
    assert profile["deterioration"] == 0.0      # identical innings


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            passed += 1
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")

    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
