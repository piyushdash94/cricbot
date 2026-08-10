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
    innings_team_name,
    over_series,
    phase_splits,
    partnership_rows,
    win_prob_series,
    cumulative_runs,
    chase_state,
    player_of_match,
)
from scorecard_charts import (
    manhattan,
    phase_table,
    partnership_bars,
    win_probability,
    worm,
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

    Cutting inside a ``` block would leave the fence unclosed and break
    rendering for everything after it, so an odd fence count is balanced by
    appending a closing fence — and the budget reserves room for it.

    Args:
        text: Full text
        max_chars: Character limit

    Returns:
        Truncated text string, never longer than max_chars
    """
    if len(text) <= max_chars:
        return text

    # Reserve room for the ellipsis line and a possible closing fence.
    budget = max(0, max_chars - 6)

    lines = text[:budget].split("\n")
    if len(lines) > 1:
        result = "\n".join(lines[:-1]) + "\n…"
    else:
        result = text[:budget] + "…"

    if result.count("```") % 2:
        result += "\n```"

    return result


def build_compact_scorecard(
    info,
    scorecard,
    top_n=3,
    max_chars=1900,
    phases=False,
    charts=False,
    partnerships=False,
    fow=True,
):
    """
    Build a compact match scorecard optimized for Discord/Telegram.

    All optional sections read the same cached scorecard payload, so enabling
    them costs no extra HTTP requests.

    Args:
        info: match_info response
        scorecard: match_scorecard response
        top_n: Number of top batters/bowlers to show (default 3 for brevity)
        max_chars: Character limit; output is truncated to fit (default 1900)
        phases: Include powerplay/middle/death splits
        charts: Include the runs-per-over manhattan sparkline
        partnerships: Include the biggest stands as bars
        fow: Include fall of wickets (default True)

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
        team = innings_team_name(inn)
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

        # Phase splits (powerplay / middle / death)
        if phases:
            split = phase_splits(inn)
            if split:
                lines.append("")
                lines.append("Phases:")
                lines.append("```")
                lines.append(phase_table(split))
                lines.append("```")

        # Runs-per-over manhattan
        if charts:
            overs = over_series(inn)
            if overs:
                lines.append("")
                lines.append(f"Runs/over (1-{len(overs)}):")
                lines.append("```")
                lines.append(manhattan(overs))
                lines.append("```")

        # Biggest stands
        if partnerships:
            stands = partnership_rows(inn)
            if stands:
                lines.append("")
                lines.append("Top stands:")
                lines.append("```")
                lines.append(partnership_bars(stands, top_n=top_n))
                lines.append("```")

        # Live chase requirement, when this innings is an unfinished chase
        state = chase_state(inn)
        if state:
            lines.append("")
            lines.append(
                f"Need {state['required_runs']} off {state['balls_left']} "
                f"(RRR {state['required_rate']})"
            )

        # Fall of wickets
        if fow:
            wickets_fallen = extract_fow(inn)

            lines.append("")
            lines.append("Fall of wickets:")

            if not wickets_fallen:
                lines.append("- No fall-of-wickets data available")
            else:
                for w in wickets_fallen:
                    line = (
                        f"- {w['score']}/{w['wicket']} "
                        f"at {w['overs']} overs — {w['batter']}"
                    )

                    if w["dismissal"]:
                        line += f" ({w['dismissal']})"

                    lines.append(line)

    # Match-wide extras
    potm = player_of_match(scorecard)
    if potm:
        lines.append("")
        lines.append(f"Player of the Match: {potm}")

    if charts and len(innings_list) >= 2:
        first_team = innings_team_name(innings_list[0])
        curve = win_prob_series(innings_list, first_team)
        if any(p is not None for p in curve):
            lines.append("")
            lines.append(f"Win% {first_team} (whole match):")
            lines.append("```")
            lines.append(win_probability(curve))
            lines.append("```")

        lines.append("")
        lines.append("Runs worm:")
        lines.append("```")
        lines.append(
            worm(
                [cumulative_runs(i) for i in innings_list],
                labels=[innings_team_name(i) for i in innings_list],
            )
        )
        lines.append("```")

    # Join and truncate
    result_text = "\n".join(lines)
    result_text = truncate_at_limit(result_text, max_chars)

    return result_text
