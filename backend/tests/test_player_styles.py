"""Playing styles from ESPN scorecard player objects (no network)."""

from backend.db import player_styles as ps


def test_classify_reads_long_styles_then_codes():
    c = ps.classify
    assert c({"longBattingStyles": ["left-hand bat"], "longBowlingStyles": ["right-arm offbreak"]}) == \
        {"bat_hand": "left", "bowl_style": "right-arm offbreak", "bowl_kind": "spin", "bowl_arm": "right"}
    assert c({"longBattingStyles": ["right-hand bat"], "longBowlingStyles": ["left-arm fast-medium"]})["bowl_kind"] == "pace"
    assert c({"longBowlingStyles": ["slow left-arm orthodox"]})["bowl_arm"] == "left"
    assert c({"longBowlingStyles": ["legbreak googly"]}) | {} == {"bat_hand": "", "bowl_style": "legbreak googly",
                                                                  "bowl_kind": "spin", "bowl_arm": "right"}
    assert c({"battingStyles": ["lhb"], "bowlingStyles": ["lfm"]}) == {"bat_hand": "left", "bowl_style": "lfm",
                                                                       "bowl_kind": "pace", "bowl_arm": "left"}
    assert c({"battingStyles": ["rhb"], "bowlingStyles": ["lbg"]})["bowl_arm"] == "right"     # a leg-spinner, not left-arm
    assert c({"longBattingStyles": ["right-hand bat"], "longBowlingStyles": []})["bowl_kind"] == ""


def test_from_scorecard_walks_nested_players_by_cricinfo_id():
    payload = {"content": {"innings": [{"inningBatsmen": [{"player": {
        "objectId": 253802, "id": 11, "longName": "Virat Kohli", "battingStyles": ["rhb"],
        "longBattingStyles": ["right-hand bat"], "longBowlingStyles": ["right-arm medium"]}}]}],
        "matchPlayers": {"teamPlayers": [{"players": [{"player": {
            "objectId": 625383, "longName": "Jasprit Bumrah", "battingStyles": ["rhb"],
            "longBowlingStyles": ["right-arm fast"]}}]}]}}}
    found = ps.from_scorecard(payload)
    assert set(found) == {253802, 625383}                      # objectId, not ESPN's internal id
    assert found[625383]["bowl_kind"] == "pace" and found[253802]["name"] == "Virat Kohli"
