"""
Consolidated helper functions for cricket scorecard data extraction.
Handles various API response shapes and data inconsistencies.
"""

from bs4 import BeautifulSoup


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

    Args:
        html: HTML string

    Returns:
        Cleaned text string
    """
    if not html:
        return ""
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

        dismissal = (
            wicket.get("dismissalText")
            or wicket.get("dismissalComment")
            or fow.get("dismissalText")
            or fow.get("dismissalComment")
            or ""
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
