"""
Consolidated helper functions for cricket scorecard data extraction.
Handles various API response shapes and data inconsistencies.
"""


def get_nested(d, path, default="-"):
    """
    Safely access nested dictionary keys.

    Args:
        d: Dictionary to traverse
        path: List of keys to follow
        default: Value if any key is missing

    Returns:
        Value at path, or default if not found
    """
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def html_to_text(html):
    """
    Convert HTML to plain text using BeautifulSoup.

    BeautifulSoup is imported lazily so the scorecard path, which never
    touches HTML, works without beautifulsoup4 installed.

    Args:
        html: HTML string

    Returns:
        Cleaned text string
    """
    if not html:
        return ""

    from bs4 import BeautifulSoup

    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


def player_name(row, default="Unknown"):
    """
    Extract player name from various dict shapes.
    Handles multiple API response formats.

    Args:
        row: Dict with player data (batter, bowler, or wicket record)
        default: Fallback name if extraction fails

    Returns:
        Player name string
    """
    # Try nested player objects
    player = (
        row.get("player")
        or row.get("athlete")
        or row.get("batsman", {}).get("athlete")
        or row.get("bowler", {}).get("athlete")
        or row.get("dismissedBatsman", {})
        or {}
    )

    # Try name fields in priority order
    return (
        player.get("longName")
        or player.get("displayName")
        or player.get("name")
        or row.get("playerName")
        or row.get("batterName")
        or row.get("dismissedPlayer")
        or row.get("batterName")
        or default
    )


def valid_batter(row):
    """
    Filter batter records: exclude subs, require runs value.

    Args:
        row: Batter record

    Returns:
        True if valid, False otherwise
    """
    return row.get("battedType") != "sub" and row.get("runs") is not None


def valid_bowler(row):
    """
    Filter bowler records: require overs value.

    Args:
        row: Bowler record

    Returns:
        True if valid, False otherwise
    """
    return row.get("overs") is not None


def get_bowler_runs(row):
    """
    Extract runs conceded by bowler, with economy-based estimation fallback.

    Args:
        row: Bowler record

    Returns:
        Runs as int, or "-" if unavailable
    """
    # Try direct fields first
    runs = (
        row.get("runs")
        or row.get("runsConceded")
        or row.get("conceded")
        or row.get("totalRuns")
    )

    if runs is not None:
        return runs

    # Fallback: estimate from economy
    overs = row.get("overs")
    economy = row.get("economy")

    if overs is not None and economy is not None:
        try:
            return round(float(overs) * float(economy))
        except (TypeError, ValueError):
            return "-"

    return "-"


def get_match_title(info, scorecard):
    """
    Extract match title from info or scorecard.
    Prefers "TeamA vs TeamB" format if available.

    Args:
        info: match_info response
        scorecard: match_scorecard response

    Returns:
        Match title string
    """
    match = scorecard.get("match", {}) or {}

    # Try to extract team names from match.teams
    team_names = []
    for team in match.get("teams", []):
        team_data = team.get("team", {})
        abbr = team_data.get("abbreviation") or team_data.get("name")
        if abbr:
            team_names.append(abbr)

    if len(team_names) == 2:
        return f"{team_names[0]} vs {team_names[1]}"

    # Fallback to info or match fields
    return (
        info.get("title")
        or match.get("title")
        or match.get("longName")
        or "Match"
    )


def get_match_result(info, scorecard):
    """
    Extract match result/status text.

    Args:
        info: match_info response
        scorecard: match_scorecard response

    Returns:
        Status text string
    """
    match = scorecard.get("match", {}) or {}

    return (
        info.get("statusText")
        or match.get("statusText")
        or match.get("status")
        or "Result unavailable"
    )


def get_match_ground(info, scorecard):
    """
    Extract venue/ground name.

    Args:
        info: match_info response
        scorecard: match_scorecard response

    Returns:
        Venue name string
    """
    match = scorecard.get("match", {}) or {}

    return (
        info.get("ground", {}).get("name")
        or info.get("venue", {}).get("name")
        or match.get("ground", {}).get("name")
        or "Venue unavailable"
    )


def extract_over(comment):
    """
    Extract over number from commentary record.
    Handles various dict shapes.

    Args:
        comment: Commentary record

    Returns:
        Over number (string or "-")
    """
    over = comment.get("over")

    if isinstance(over, dict):
        return (
            over.get("overNumber")
            or over.get("overs")
            or over.get("over")
            or "-"
        )

    return (
        over
        or comment.get("overNumber")
        or comment.get("ballOvers")
        or comment.get("overs")
        or "-"
    )


def extract_text_items(comment, key):
    """
    Extract text from commentary items array (HTML → text).

    Args:
        comment: Commentary record
        key: Key to look up (e.g., "commentPreTextItems")

    Returns:
        List of text strings
    """
    items = comment.get(key, []) or []

    texts = []
    for item in items:
        if item.get("type") == "HTML":
            text = html_to_text(item.get("html"))
            if text:
                texts.append(text)

    return texts


def extract_commentary_text(comment):
    """
    Build full commentary text from pre/main/post items.

    Args:
        comment: Commentary record

    Returns:
        Full text string
    """
    pre = extract_text_items(comment, "commentPreTextItems")
    main = extract_text_items(comment, "commentTextItems")
    post = extract_text_items(comment, "commentPostTextItems")

    return " ".join(pre + main + post).strip()


def extract_player_name_from_id(comment, player_id_key):
    """
    Extract player name from commentary, fallback to title parsing.

    Args:
        comment: Commentary record
        player_id_key: Key to check (e.g., "batsmanPlayerId")

    Returns:
        Player name string
    """
    player_id = comment.get(player_id_key)

    if not player_id:
        return "-"

    # Fallback: parse title (e.g., "Hazlewood to Shashank Singh")
    title = comment.get("title", "")
    if " to " in title:
        bowler, batter = title.split(" to ", 1)

        if player_id_key == "bowlerPlayerId":
            return bowler.strip()

        if player_id_key == "batsmanPlayerId":
            return batter.strip()

    return str(player_id)


def get_fow_player(w):
    """
    Extract batter name from fall-of-wickets record.

    Args:
        w: FOW or wicket record

    Returns:
        Batter name string
    """
    player = (
        w.get("player")
        or w.get("athlete")
        or w.get("batsman")
        or w.get("dismissedBatsman")
        or {}
    )

    return (
        player.get("longName")
        or player.get("displayName")
        or player.get("name")
        or w.get("playerName")
        or w.get("batterName")
        or w.get("dismissedPlayer")
        or "Unknown"
    )


def dismissal_text(value):
    """
    Normalize a dismissal field to a plain string.

    Cricinfo returns dismissalText either as a string or as a dict with
    short/long/commentary variants; the long form reads best in a scorecard.

    Args:
        value: String, dict, or None

    Returns:
        Dismissal string ("" if unavailable)
    """
    if not value:
        return ""

    if isinstance(value, dict):
        return (
            value.get("long")
            or value.get("commentary")
            or value.get("short")
            or ""
        )

    return str(value)


def extract_fow(inn):
    """
    Reconstruct Fall of Wickets from innings data.
    Merges inningFallOfWickets and inningWickets for richer dismissal info.

    Args:
        inn: Innings record

    Returns:
        List of dicts: [{"score", "wicket", "overs", "batter", "dismissal"}, ...]
    """
    fow_rows = inn.get("inningFallOfWickets", []) or []
    wicket_rows = inn.get("inningWickets", []) or []

    results = []

    for idx, fow in enumerate(fow_rows):
        wicket = wicket_rows[idx] if idx < len(wicket_rows) else {}

        score = (
            fow.get("score")
            or fow.get("teamScore")
            or fow.get("runs")
            or fow.get("totalRuns")
            or fow.get("fowRuns")
            or fow.get("fowScore")
            or wicket.get("score")
            or wicket.get("teamScore")
            or wicket.get("runs")
            or wicket.get("totalRuns")
            or wicket.get("inningRuns")
            or "-"
        )

        wicket_no = (
            fow.get("wicketNumber")
            or fow.get("wicketNum")
            or fow.get("wickets")
            or fow.get("fowWicketNum")
            or wicket.get("wicketNumber")
            or wicket.get("wicketNum")
            or wicket.get("wickets")
            or idx + 1
        )

        overs = (
            fow.get("overs")
            or fow.get("over")
            or fow.get("fowOvers")
            or wicket.get("overs")
            or wicket.get("over")
            or wicket.get("ballOvers")
            or "-"
        )

        batter = get_fow_player(wicket)
        if batter == "Unknown":
            batter = get_fow_player(fow)

        dismissal = dismissal_text(
            wicket.get("dismissalText")
            or wicket.get("dismissalComment")
            or fow.get("dismissalText")
            or fow.get("dismissalComment")
        )

        results.append(
            {
                "score": score,
                "wicket": wicket_no,
                "overs": overs,
                "batter": batter,
                "dismissal": dismissal,
            }
        )

    return results


# ----------------------------------------------------------------------
# Innings analytics — all served by the same cached scorecard fetch
#
# cricdata caches the scorecard page per (series_slug, match_slug), so
# match_scorecard / match_info / match_overs / match_partnerships /
# match_fall_of_wickets all share one HTTP request. Everything below reads
# that same payload and therefore costs nothing extra.
# ----------------------------------------------------------------------

PHASE_LABELS = {
    "POWERPLAY": "Powerplay",
    "MIDDLE_OVERS": "Middle",
    "FINAL_OVERS": "Death",
}


def short_name(player, default="?"):
    """
    Extract a surname-style short name for tight layouts.

    Cricinfo supplies fieldingName ("Narine") alongside name ("SP Narine"),
    which is what chart labels want. Falls back to the last token.

    Args:
        player: Player dict
        default: Fallback when nothing usable is present

    Returns:
        Short name string
    """
    if not isinstance(player, dict):
        return default

    short = player.get("fieldingName") or player.get("mobileName")
    if short:
        return short

    full = player.get("name") or player.get("longName")
    if full:
        return full.split()[-1]

    return default


def innings_team_name(inn, default="Unknown Team"):
    """
    Extract the batting team's short name for an innings.

    Args:
        inn: Innings record
        default: Fallback when no name is present

    Returns:
        Team abbreviation or name
    """
    team = inn.get("team") or {}
    return (
        team.get("abbreviation")
        or team.get("name")
        or team.get("longName")
        or default
    )


def over_series(inn):
    """
    Flatten an innings' over-by-over progression.

    Args:
        inn: Innings record

    Returns:
        List of dicts with over, runs, wickets, total_runs, total_wickets,
        run_rate, required_runs, required_rate, balls_left, win_prob,
        projected
    """
    rows = []
    for ov in inn.get("inningOvers", []) or []:
        pred = ov.get("predictions") or {}
        rows.append(
            {
                "over": ov.get("overNumber"),
                "runs": ov.get("overRuns"),
                "wickets": ov.get("overWickets"),
                "total_runs": ov.get("totalRuns"),
                "total_wickets": ov.get("totalWickets"),
                "run_rate": ov.get("overRunRate"),
                "required_runs": ov.get("requiredRuns"),
                "required_rate": ov.get("requiredRunRate"),
                "balls_left": ov.get("remainingBalls"),
                "win_prob": pred.get("winProbability"),
                "projected": pred.get("score"),
            }
        )

    # Cricinfo returns overs newest-first on live matches.
    rows.sort(key=lambda r: r["over"] if r["over"] is not None else 0)
    return rows


def win_prob_series(innings_list, team_name):
    """
    Build a match-long win-probability curve for one team.

    Cricinfo reports winProbability for whichever team is BATTING in that
    innings. Verified on a completed match: the chasing side's innings ends
    at 100 and the side that lost ends its own innings well under 50. So to
    follow a single team across the whole match, the innings where they
    bowled must be inverted.

    Args:
        innings_list: scorecard["content"]["innings"]
        team_name: Team to track, matched against innings_team_name()

    Returns:
        List of percentages for that team, in match order
    """
    series = []
    for inn in innings_list:
        batting = innings_team_name(inn)
        for row in over_series(inn):
            wp = row["win_prob"]
            if wp is None:
                series.append(None)
            elif batting == team_name:
                series.append(wp)
            else:
                series.append(100.0 - wp)

    return series


def phase_splits(inn):
    """
    Powerplay / middle / death aggregates for an innings.

    Reads inningOverGroups, which Cricinfo pre-aggregates — no need to
    bucket overs by hand.

    Args:
        inn: Innings record

    Returns:
        List of dicts with name, start_over, end_over, runs, wickets
    """
    phases = []
    for g in inn.get("inningOverGroups", []) or []:
        raw = g.get("type") or ""
        phases.append(
            {
                "name": PHASE_LABELS.get(raw, raw.replace("_", " ").title()),
                "start_over": g.get("startOverNumber"),
                "end_over": g.get("endOverNumber"),
                # oversRuns is this phase alone; totalRuns is cumulative.
                "runs": g.get("oversRuns") or 0,
                "wickets": g.get("oversWickets") or 0,
            }
        )

    return phases


def partnership_rows(inn):
    """
    Normalize an innings' partnerships.

    Args:
        inn: Innings record

    Returns:
        List of dicts with runs, balls, overs, both players' names and
        contributions, and is_live
    """
    rows = []
    for p in inn.get("inningPartnerships", []) or []:
        p1 = p.get("player1") or {}
        p2 = p.get("player2") or {}

        rows.append(
            {
                "runs": p.get("runs") or 0,
                "balls": p.get("balls") or 0,
                "overs": p.get("overs"),
                "player1": p1.get("longName") or p1.get("name") or "?",
                "player2": p2.get("longName") or p2.get("name") or "?",
                "player1_short": short_name(p1),
                "player2_short": short_name(p2),
                "player1_runs": p.get("player1Runs") or 0,
                "player1_balls": p.get("player1Balls") or 0,
                "player2_runs": p.get("player2Runs") or 0,
                "player2_balls": p.get("player2Balls") or 0,
                "is_live": bool(p.get("isLive")),
            }
        )

    return rows


def cumulative_runs(inn):
    """
    Cumulative run total after each over, for worm charts.

    Args:
        inn: Innings record

    Returns:
        List of running totals
    """
    return [
        r["total_runs"] for r in over_series(inn) if r["total_runs"] is not None
    ]


def chase_state(inn):
    """
    Chase requirement from the final recorded over of an innings.

    Args:
        inn: Innings record

    Returns:
        Dict with required_runs, balls_left, required_rate — or None when the
        innings is not a chase
    """
    rows = over_series(inn)
    if not rows:
        return None

    last = rows[-1]
    if not last.get("required_runs"):
        return None

    return {
        "required_runs": last["required_runs"],
        "balls_left": last["balls_left"],
        "required_rate": last["required_rate"],
    }


def player_of_match(scorecard):
    """
    Extract the Player of the Match name.

    Args:
        scorecard: match_scorecard response

    Returns:
        Player name, or None when unavailable
    """
    content = scorecard.get("content", {}) or {}
    support = content.get("supportInfo") or {}

    candidates = (
        support.get("playersOfTheMatch")
        or content.get("matchPlayerAwards")
        or []
    )

    for entry in candidates:
        name = player_name(entry, default=None)
        if name:
            return name

    return None

