"""
Tests for the Statsguru access layer and venue profiling.

Statsguru payloads are synthesized here with the quirks the real scraper
produces: every value a string, "-" for missing, "*" on not-out scores, and
column names that vary between formats.

    pytest            # or: python tests/test_venue.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cricbot_stats import (  # noqa: E402
    to_number, numeric_rows, ground_id_from_match, ground_name_from_match,
    resolve_player, safe_call,
)
from cricbot_venue import (  # noqa: E402
    venue_profile, venue_bars, format_venue_card, player_at_venue,
    combine_match_and_history, format_pitch_read,
)


# ---------------------------------------------------------------- fixtures


GROUND_STATS = {
    "summary": {
        "Mat": "82", "Won": "40", "Lost": "38", "Drawn": "4",
        "Ave": "165.4", "RPO": "8.31",
    },
    "breakdowns": [
        {"Team": "India", "Mat": "30", "Won": "18", "Lost": "12",
         "Ave": "170.2", "RPO": "8.55"},
        {"Team": "Australia", "Mat": "12", "Won": "5", "Lost": "7",
         "Ave": "-", "RPO": "7.90"},
    ],
}

PLAYER_GROUNDS = {
    "summary": {"Mat": "40", "Runs": "1800", "Ave": "48.6"},
    "grounds": [
        {"Ground": "Eden Gardens, Kolkata", "Mat": "12", "Runs": "640",
         "Ave": "58.18", "SR": "142.2", "HS": "113*"},
        {"Ground": "Wankhede Stadium", "Mat": "8", "Runs": "310",
         "Ave": "38.75", "SR": "131.0", "HS": "88"},
    ],
}

SCORECARD = {
    "match": {
        "ground": {"id": 292, "objectId": 57980,
                   "name": "Eden Gardens, Kolkata"},
    }
}

MATCH_PITCH = {
    "innings_rpo": [8.70, 10.84],
    "spin_minus_pace_econ": -1.31,
    "deterioration": 2.14,
}


# ------------------------------------------------------------ to_number


def test_to_number_plain_values():
    assert to_number("46.85") == 46.85
    assert to_number("123") == 123
    assert to_number(42) == 42
    assert to_number(3.5) == 3.5


def test_to_number_statsguru_annotations():
    assert to_number("254*") == 254        # not-out marker
    assert to_number("19+") == 19          # aggregate marker
    assert to_number("1,250") == 1250      # thousands separator


def test_to_number_missing_values():
    assert to_number("-") is None
    assert to_number("") is None
    assert to_number("   ") is None
    assert to_number(None) is None
    assert to_number("did not bat") is None


def test_numeric_rows_leaves_unknown_columns_alone():
    rows = [{"Mat": "12", "Team": "India", "Ave": "-"}]
    out = numeric_rows(rows)
    assert out[0]["Mat"] == 12
    assert out[0]["Team"] == "India"       # unparseable, kept as-is
    assert out[0]["Ave"] == "-"            # missing, original preserved


def test_numeric_rows_respects_key_filter():
    rows = [{"Mat": "12", "Ave": "48.6"}]
    out = numeric_rows(rows, keys=["Mat"])
    assert out[0]["Mat"] == 12
    assert out[0]["Ave"] == "48.6"         # not in keys, untouched


def test_numeric_rows_tolerates_junk():
    assert numeric_rows(None) == []
    assert numeric_rows([None, "x", {"a": "1"}]) == [{"a": 1}]


# ------------------------------------------------------------- ids


def test_ground_id_uses_id_not_objectid():
    """Statsguru wants ground.id (292); objectId (57980) will not resolve."""
    assert ground_id_from_match(SCORECARD) == 292


def test_ground_id_absent():
    assert ground_id_from_match({}) is None
    assert ground_id_from_match({"match": {"ground": {}}}) is None


def test_ground_name():
    assert ground_name_from_match(SCORECARD) == "Eden Gardens, Kolkata"
    assert ground_name_from_match({}) is None


def test_resolve_player_flags_ambiguity():
    class FakeClient:
        def search_players(self, name, limit=5):
            return [{"id": "253802", "displayName": "Virat Kohli"},
                    {"id": "999", "displayName": "Virat Singh"}]

    got = resolve_player(FakeClient(), "Virat")
    assert got["id"] == "253802"
    assert got["ambiguous"] is True
    assert len(got["candidates"]) == 2


def test_resolve_player_no_match():
    class Empty:
        def search_players(self, name, limit=5):
            return []

    assert resolve_player(Empty(), "Nobody") is None


def test_safe_call_swallows_failures():
    def boom():
        raise RuntimeError("statsguru markup changed")

    assert safe_call(boom) is None
    assert safe_call(lambda x: x * 2, 21) == 42


# --------------------------------------------------------- venue_profile


def test_venue_profile_parses_summary():
    p = venue_profile(GROUND_STATS, name="Eden Gardens")
    assert p["matches"] == 82
    assert p["won"] == 40
    assert p["average"] == 165.4
    assert p["rpo"] == 8.31
    assert p["missing"] == []


def test_venue_profile_breakdowns():
    p = venue_profile(GROUND_STATS)
    assert [t["team"] for t in p["teams"]] == ["India", "Australia"]
    assert p["teams"][0]["matches"] == 30
    assert p["teams"][1]["average"] is None      # "-" in the source


def test_venue_profile_reports_missing_fields():
    p = venue_profile({"summary": {"Mat": "10"}})
    assert p["matches"] == 10
    assert "average" in p["missing"] and "rpo" in p["missing"]


def test_venue_profile_handles_empty_input():
    for empty in (None, {}, {"summary": {}}):
        p = venue_profile(empty)
        assert p["matches"] is None
        assert p["missing"]                       # never silently fine


def test_venue_profile_alternate_column_names():
    """Column names differ across formats; candidates must be tried."""
    p = venue_profile({"summary": {"Matches": "50", "Avg": "300",
                                   "Run Rate": "3.2"}})
    assert p["matches"] == 50
    assert p["average"] == 300
    assert p["rpo"] == 3.2


# ------------------------------------------------------------- rendering


def test_venue_bars_aligned_and_placeholder():
    bars = venue_bars(venue_profile(GROUND_STATS), width=10)
    assert "Won" in bars and "Lost" in bars
    assert all(len(line.split("█")[0]) >= 7 for line in bars.splitlines())
    assert venue_bars({}).startswith("(")


def test_format_venue_card():
    out = format_venue_card(venue_profile(GROUND_STATS, name="Eden Gardens"))
    assert "Eden Gardens" in out
    assert "82 matches" in out
    assert "RPO 8.31" in out
    assert out.count("```") % 2 == 0


def test_format_venue_card_empty():
    assert "No venue statistics" in format_venue_card(venue_profile(None))
    assert "No venue statistics" in format_venue_card(None)


def test_format_venue_card_notes_missing_fields():
    out = format_venue_card(venue_profile({"summary": {"Mat": "10"}}))
    assert "unavailable" in out


# ---------------------------------------------------------- player_at_venue


def test_player_at_venue_filters_by_ground():
    got = player_at_venue(PLAYER_GROUNDS, "V Kohli", "Eden Gardens")
    assert len(got["rows"]) == 1
    assert got["rows"][0]["average"] == 58.18
    assert got["rows"][0]["highest"] == 113        # "113*" stripped


def test_player_at_venue_all_grounds():
    assert len(player_at_venue(PLAYER_GROUNDS)["rows"]) == 2


def test_player_at_venue_no_match():
    assert player_at_venue(PLAYER_GROUNDS, ground_name="Lord's")["rows"] == []
    assert player_at_venue(None)["rows"] == []


# -------------------------------------------------------------- pitch read


def test_combine_computes_deviation():
    c = combine_match_and_history(MATCH_PITCH, venue_profile(GROUND_STATS))
    assert c["match_rpo"] == 9.77                  # mean of 8.70 and 10.84
    assert c["venue_rpo"] == 8.31
    assert c["deviation"] == 1.46
    assert c["confidence"] == "good"               # n=82


def test_combine_confidence_scales_with_sample():
    for n, expected in ((None, "none"), (5, "low"), (20, "moderate"),
                        (50, "good")):
        venue = venue_profile({"summary": {"Mat": str(n), "RPO": "8.0"}}
                              if n else None)
        assert combine_match_and_history(MATCH_PITCH, venue)["confidence"] \
            == expected


def test_combine_distinguishes_no_baseline_from_no_deviation():
    """None means 'could not compare', not 'no difference'."""
    c = combine_match_and_history(MATCH_PITCH, venue_profile(None))
    assert c["match_rpo"] == 9.77
    assert c["venue_rpo"] is None
    assert c["deviation"] is None


def test_combine_handles_empty_match():
    c = combine_match_and_history({}, venue_profile(GROUND_STATS))
    assert c["match_rpo"] is None
    assert c["deviation"] is None


def test_format_pitch_read():
    c = combine_match_and_history(MATCH_PITCH, venue_profile(GROUND_STATS))
    out = format_pitch_read(c, MATCH_PITCH)
    assert "9.77 RPO" in out
    assert "above the venue baseline" in out
    assert "n=82" in out
    assert "favoured spin" in out                  # negative spin-pace gap


def test_format_pitch_read_without_baseline():
    c = combine_match_and_history(MATCH_PITCH, venue_profile(None))
    out = format_pitch_read(c)
    assert "No venue baseline available" in out


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
