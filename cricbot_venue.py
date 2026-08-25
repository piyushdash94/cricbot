"""
Venue and pitch profiling.

cricdata's ground_stats() returns venue aggregates in a single call, which
replaces backfilling a season of scorecards to characterise a ground.

Two caveats worth keeping in view:

- ground_stats is type=team data — venue totals broken down by team. It is
  not a pitch model. The pitch reading is derived here.
- A single match is not a pitch rating. combine_match_and_history() exists
  so one match is read as a deviation from the venue's baseline rather than
  as a verdict on its own.
"""

from cricbot_stats import to_number
from cricbot_views import _fit
from scorecard_charts import hbar

# Statsguru column names vary by format and stat type, so each field is
# looked up through a list of candidates rather than one hard-coded key.
_MATCHES_KEYS = ("Mat", "Matches", "Span")
_WON_KEYS = ("Won", "W", "Win")
_LOST_KEYS = ("Lost", "L", "Loss")
_DRAWN_KEYS = ("Drawn", "D", "Draw", "Tied")
_AVE_KEYS = ("Ave", "Avg", "Average", "RpW")
_RPO_KEYS = ("RPO", "Run Rate", "RunRate", "SR")


def _first_number(row, keys):
    """Return the first parseable number among candidate column names."""
    for key in keys:
        if key in row:
            number = to_number(row[key])
            if number is not None:
                return number
    return None


def venue_profile(ground, name=None):
    """
    Normalize a ground_stats() response into a usable venue summary.

    Args:
        ground: ground_stats() response, or None when the lookup failed
        name: Optional venue name for display

    Returns:
        Dict with name, matches, won, lost, drawn, average, rpo, teams and
        missing (the fields that could not be found). Returns a profile with
        n=0 rather than raising when the payload is empty or unrecognized.
    """
    profile = {
        "name": name,
        "matches": None,
        "won": None,
        "lost": None,
        "drawn": None,
        "average": None,
        "rpo": None,
        "teams": [],
        "missing": [],
    }

    if not ground:
        profile["missing"] = ["all"]
        return profile

    summary = ground.get("summary") or {}
    profile["matches"] = _first_number(summary, _MATCHES_KEYS)
    profile["won"] = _first_number(summary, _WON_KEYS)
    profile["lost"] = _first_number(summary, _LOST_KEYS)
    profile["drawn"] = _first_number(summary, _DRAWN_KEYS)
    profile["average"] = _first_number(summary, _AVE_KEYS)
    profile["rpo"] = _first_number(summary, _RPO_KEYS)

    for field in ("matches", "average", "rpo"):
        if profile[field] is None:
            profile["missing"].append(field)

    for row in ground.get("breakdowns") or []:
        team = (
            row.get("Team")
            or row.get("Opposition")
            or row.get("Grouping")
        )
        if not team:
            continue
        profile["teams"].append({
            "team": team,
            "matches": _first_number(row, _MATCHES_KEYS),
            "won": _first_number(row, _WON_KEYS),
            "lost": _first_number(row, _LOST_KEYS),
            "average": _first_number(row, _AVE_KEYS),
            "rpo": _first_number(row, _RPO_KEYS),
        })

    return profile


def venue_bars(profile, width=20):
    """
    Win/loss split for the venue as horizontal bars.

    Args:
        profile: venue_profile() result
        width: Bar width

    Returns:
        Bar lines, or a placeholder when the record is unavailable
    """
    won, lost = profile.get("won"), profile.get("lost")
    if won is None and lost is None:
        return "(no result breakdown available)"

    won = won or 0
    lost = lost or 0
    drawn = profile.get("drawn") or 0
    ceiling = max(won, lost, drawn, 1)

    lines = []
    for label, value in (("Won", won), ("Lost", lost), ("Drawn", drawn)):
        lines.append(f"{_fit(label, 7)}{hbar(value, ceiling, width=width)} {value}")

    return "\n".join(lines)


def format_venue_card(profile):
    """
    Render a venue profile as a chat-ready card.

    Args:
        profile: venue_profile() result

    Returns:
        Formatted string
    """
    if not profile or profile.get("missing") == ["all"]:
        return "No venue statistics available."

    name = profile.get("name") or "Venue"
    lines = [f"🏟 {name}"]

    matches = profile.get("matches")
    if matches is not None:
        lines.append(f"{matches} matches on record")

    stats = []
    if profile.get("average") is not None:
        stats.append(f"avg {profile['average']}")
    if profile.get("rpo") is not None:
        stats.append(f"RPO {profile['rpo']}")
    if stats:
        lines.append(" | ".join(stats))

    bars = venue_bars(profile)
    if not bars.startswith("("):
        lines.append("")
        lines.append("```")
        lines.append(bars)
        lines.append("```")

    if profile.get("missing"):
        lines.append("")
        lines.append(f"(unavailable: {', '.join(profile['missing'])})")

    return "\n".join(lines)


def player_at_venue(stats, player_name=None, ground_name=None):
    """
    Pull one venue's row out of a player_ground_stats() response.

    Args:
        stats: player_ground_stats() response, or None
        player_name: Optional name for display
        ground_name: Venue to match against the Ground column; None returns
            every venue row

    Returns:
        Dict with player, ground, and the matched rows (numeric where
        possible). Rows is empty when nothing matched.
    """
    result = {"player": player_name, "ground": ground_name, "rows": []}
    if not stats:
        return result

    for row in stats.get("grounds") or []:
        ground = row.get("Ground") or row.get("Grouping") or ""
        if ground_name and ground_name.lower() not in str(ground).lower():
            continue

        result["rows"].append({
            "ground": ground,
            "matches": _first_number(row, _MATCHES_KEYS),
            "runs": to_number(row.get("Runs")),
            "average": _first_number(row, _AVE_KEYS),
            "strike_rate": to_number(row.get("SR")),
            "highest": to_number(row.get("HS")),
        })

    return result


def combine_match_and_history(match_profile, venue):
    """
    Read a single match's pitch signals against the venue's baseline.

    One match is noise; the same match expressed as a deviation from the
    ground's long-run scoring rate is a signal. Deviation is None when either
    side is unavailable, so callers can tell "no baseline" from "no
    deviation".

    Args:
        match_profile: impact_lab.pitch_profile() result
        venue: venue_profile() result

    Returns:
        Dict with match_rpo, venue_rpo, deviation, sample_size and a
        confidence label
    """
    innings_rpo = (match_profile or {}).get("innings_rpo") or []
    match_rpo = (
        round(sum(innings_rpo) / len(innings_rpo), 2) if innings_rpo else None
    )
    venue_rpo = (venue or {}).get("rpo")
    sample = (venue or {}).get("matches")

    deviation = None
    if match_rpo is not None and venue_rpo:
        deviation = round(match_rpo - venue_rpo, 2)

    # Venue baselines below ~10 matches are too noisy to read as a pitch
    # characteristic rather than as variation between teams.
    if not sample:
        confidence = "none"
    elif sample < 10:
        confidence = "low"
    elif sample < 30:
        confidence = "moderate"
    else:
        confidence = "good"

    return {
        "match_rpo": match_rpo,
        "venue_rpo": venue_rpo,
        "deviation": deviation,
        "sample_size": sample,
        "confidence": confidence,
    }


def format_pitch_read(combined, match_profile=None):
    """
    Render the match-versus-venue comparison.

    Args:
        combined: combine_match_and_history() result
        match_profile: Optional pitch_profile() result for the pace/spin split

    Returns:
        Formatted string
    """
    lines = ["🏏 PITCH READ"]

    match_rpo = combined.get("match_rpo")
    venue_rpo = combined.get("venue_rpo")
    deviation = combined.get("deviation")

    if match_rpo is not None:
        lines.append(f"This match: {match_rpo} RPO")
    if venue_rpo is not None:
        lines.append(f"Venue average: {venue_rpo} RPO")

    if deviation is not None:
        direction = "above" if deviation > 0 else "below"
        lines.append(f"{abs(deviation)} {direction} the venue baseline")
    elif venue_rpo is None:
        lines.append("No venue baseline available — match figures only")

    sample = combined.get("sample_size")
    lines.append(
        f"Confidence: {combined.get('confidence')}"
        + (f" (n={sample})" if sample else "")
    )

    if match_profile:
        spin_gap = match_profile.get("spin_minus_pace_econ")
        if spin_gap is not None:
            favoured = "spin" if spin_gap < 0 else "pace"
            lines.append(f"Economy favoured {favoured} by {abs(spin_gap)}")

        deterioration = match_profile.get("deterioration")
        if deterioration is not None:
            lines.append(f"2nd-innings RPO shift: {deterioration:+}")

    return "\n".join(lines)
