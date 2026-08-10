"""
Compact cricket scorecard renderer.
Builds brief, Discord-friendly match summaries with key stats.
"""

from scorecard_helpers import (
    player_name,
    valid_batter,
    valid_bowler,
    get_bowler_runs,
    get_match_title,
    get_match_result,
    get_match_ground,
    extract_fow,
)


def format_number(value):
    """
    Format a number, defaulting to "-" if missing.

    Args:
        value: Number or None

    Returns:
        String representation
    """
    if value is None:
        return "-"
    return str(value)


def truncate_at_limit(text, max_chars=1900):
    """
    Truncate text to max_chars, respecting line boundaries.
    Append "…" if truncated.

    Args:
        text: Full text
        max_chars: Character limit

    Returns:
        Truncated text string
    """
    if len(text) <= max_chars:
        return text

    # Try to cut at a line break near the limit
    lines = text[:max_chars].split("\n")
    if len(lines) > 1:
        # Remove last line (likely incomplete) and rejoin
        result = "\n".join(lines[:-1]) + "\n…"
    else:
        result = text[:max_chars - 1] + "…"

    return result


def build_compact_scorecard(info, scorecard, top_n=3, max_chars=1900):
    """
    Build a compact match scorecard optimized for Discord/Telegram.

    Args:
        info: match_info response
        scorecard: match_scorecard response
        top_n: Number of top batters/bowlers to show (default 3 for brevity)
        max_chars: Character limit; truncate FOW if needed (default 1900)

    Returns:
        Compact scorecard string
    """
    lines = []

    # Header
    title = get_match_title(info, scorecard)
    result = get_match_result(info, scorecard)
    ground = get_match_ground(info, scorecard)

    lines.append("🏏 MATCH")
    lines.append(str(title))
    lines.append(str(result))
    lines.append(str(ground))

    # Process each innings
    innings_list = scorecard.get("content", {}).get("innings", [])

    for inn in innings_list:
        team = (
            inn.get("team", {}).get("abbreviation")
            or inn.get("team", {}).get("name")
            or "Unknown Team"
        )
        runs = format_number(inn.get("runs"))
        wickets = format_number(inn.get("wickets"))
        overs = format_number(inn.get("overs"))

        extras = format_number(inn.get("extras"))
        fours = format_number(inn.get("fours"))
        sixes = format_number(inn.get("sixes"))

        lines.append("")
        lines.append(f"{team}: {runs}/{wickets} in {overs} overs")
        lines.append(f"Extras: {extras} | 4s: {fours} | 6s: {sixes}")

        # Top batters
        batters = [b for b in inn.get("inningBatsmen", []) if valid_batter(b)]
        top_batters = sorted(
            batters,
            key=lambda x: x.get("runs", 0) or 0,
            reverse=True,
        )[:top_n]

        lines.append("")
        lines.append("Top batters:")

        if not top_batters:
            lines.append("- No batting data available")
        else:
            for b in top_batters:
                name = player_name(b)
                batter_runs = format_number(b.get("runs"))
                balls = format_number(b.get("balls"))
                batter_fours = format_number(b.get("fours"))
                batter_sixes = format_number(b.get("sixes"))
                strike_rate = format_number(
                    b.get("strikerate") or b.get("strikeRate")
                )

                lines.append(
                    f"- {name}: {batter_runs}({balls}) "
                    f"4s:{batter_fours} 6s:{batter_sixes} SR:{strike_rate}"
                )

        # Top bowlers
        bowlers = [b for b in inn.get("inningBowlers", []) if valid_bowler(b)]

        top_bowlers = sorted(
            bowlers,
            key=lambda x: (
                x.get("wickets") or 0,
                -(
                    get_bowler_runs(x)
                    if isinstance(get_bowler_runs(x), int)
                    else 999
                ),
            ),
            reverse=True,
        )[:top_n]

        lines.append("")
        lines.append("Top bowlers:")

        if not top_bowlers:
            lines.append("- No bowling data available")
        else:
            for b in top_bowlers:
                name = player_name(b)
                wickets = format_number(b.get("wickets"))
                bowler_runs = get_bowler_runs(b)
                bowler_overs = format_number(b.get("overs"))
                economy = format_number(b.get("economy"))

                lines.append(
                    f"- {name}: {wickets}/{bowler_runs} "
                    f"in {bowler_overs} overs, E:{economy}"
                )

        # Fall of wickets
        fow = extract_fow(inn)

        lines.append("")
        lines.append("Fall of wickets:")

        if not fow:
            lines.append("- No fall-of-wickets data available")
        else:
            for w in fow:
                line = (
                    f"- {w['score']}/{w['wicket']} "
                    f"at {w['overs']} overs — {w['batter']}"
                )

                if w["dismissal"]:
                    line += f" ({w['dismissal']})"

                lines.append(line)

    # Join and truncate
    result_text = "\n".join(lines)
    result_text = truncate_at_limit(result_text, max_chars)

    return result_text
