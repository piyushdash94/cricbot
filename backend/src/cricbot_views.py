"""
Chat-ready views built on cricdata endpoints the project wasn't using yet.

Each formatter takes an already-fetched payload and returns a string, so it
stays testable without network access. The fetch helpers at the bottom wrap
CricinfoClient / AsyncCricinfoClient.
"""

from .scorecard_helpers import get_nested, short_name

DISCORD_LIMIT = 1900


def _fit(text, size):
    """Pad or truncate text to exactly `size` characters."""
    text = str(text)
    if len(text) <= size:
        return text.ljust(size)
    return text[: max(0, size - 1)] + "…"


# ----------------------------------------------------------------------
# Live matches — ci.live_matches()
# ----------------------------------------------------------------------


def format_live_matches(matches, limit=10):
    """
    Render the live/recent match list.

    Args:
        matches: live_matches() response
        limit: Maximum matches to list

    Returns:
        Formatted string
    """
    if not matches:
        return "No live matches right now."

    lines = ["🏏 LIVE & RECENT"]

    for m in matches[:limit]:
        teams = m.get("teams") or []
        sides = []
        for t in teams:
            abbr = get_nested(t, ["team", "abbreviation"], None) or get_nested(
                t, ["team", "name"], "?"
            )
            score = t.get("score")
            sides.append(f"{abbr} {score}" if score else str(abbr))

        title = m.get("title") or ""
        series = get_nested(m, ["series", "longName"], "") or get_nested(
            m, ["series", "name"], ""
        )
        status = m.get("statusText") or m.get("status") or ""

        lines.append("")
        lines.append(" vs ".join(sides) if sides else title)
        if series:
            lines.append(f"  {series} · {title}")
        if status:
            lines.append(f"  {status}")

    return "\n".join(lines)


# ----------------------------------------------------------------------
# Points table — ci.series_standings()
# ----------------------------------------------------------------------


def format_standings(standings, limit=12):
    """
    Render a series points table.

    Accepts either the series_standings() response (groups under "data") or
    the seriesStandings block embedded in a scorecard's supportInfo (groups
    at the top level).

    Field names verified against real IPL 2025 data: the team object is
    "teamInfo" (not "team") and net run rate is "nrr" (not "netRunRate").

    Args:
        standings: series_standings() response or a supportInfo standings block
        limit: Maximum rows per group

    Returns:
        Formatted string
    """
    groups = (
        get_nested(standings, ["data", "groups"], None)
        or standings.get("groups")
        or []
    )
    if not groups:
        return "No standings available for this series."

    lines = ["🏏 POINTS TABLE", "```", "Team    P  W  L  Pts    NRR"]

    for group in groups:
        name = group.get("name")
        if name and len(groups) > 1:
            lines.append(f"-- {name} --")

        rows = sorted(
            group.get("teamStats") or [],
            key=lambda r: r.get("rank") or 999,
        )

        for row in rows[:limit]:
            team = get_nested(row, ["teamInfo", "abbreviation"], None) or (
                get_nested(row, ["teamInfo", "name"], "?")
            )
            nrr = row.get("nrr")
            nrr_text = f"{nrr:+.3f}" if isinstance(nrr, (int, float)) else "-"

            lines.append(
                f"{_fit(team, 7)}"
                f"{str(row.get('matchesPlayed', '-')):>2} "
                f"{str(row.get('matchesWon', '-')):>2} "
                f"{str(row.get('matchesLost', '-')):>2} "
                f"{str(row.get('points', '-')):>4} "
                f"{nrr_text:>6}"
            )

    lines.append("```")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Player card — search_players + player_bio + player_career_stats
# ----------------------------------------------------------------------


def format_player_card(bio, career=None, fmt="test"):
    """
    Render a player profile with optional career stats.

    Args:
        bio: player_bio() response
        career: Optional player_career_stats() response
        fmt: Format label shown alongside the stats

    Returns:
        Formatted string
    """
    if not bio:
        return "Player not found."

    name = bio.get("fullName") or bio.get("displayName") or "Unknown player"
    lines = [f"🏏 {name}"]

    team = get_nested(bio, ["team", "name"], None)
    role = get_nested(bio, ["position", "name"], None)
    meta = [x for x in (team, role) if x]
    if meta:
        lines.append(" · ".join(meta))

    dob = bio.get("displayDOB")
    age = bio.get("age")
    if dob:
        lines.append(f"Born {dob}" + (f" (age {age})" if age else ""))

    bat = [s.get("description") for s in (bio.get("batStyle") or []) if s]
    bowl = [s.get("description") for s in (bio.get("bowlStyle") or []) if s]
    if bat:
        lines.append(f"Bats: {', '.join(bat)}")
    if bowl:
        lines.append(f"Bowls: {', '.join(bowl)}")

    if career:
        summary = career.get("summary") or {}
        if summary:
            lines.append("")
            lines.append(f"{fmt.upper()} career:")
            lines.append("```")
            # Statsguru returns every value as a string, keyed by its column.
            for key in ("Mat", "Inns", "Runs", "HS", "Ave", "SR", "100", "50",
                        "Wkts", "BBI", "Econ"):
                if key in summary:
                    lines.append(f"{_fit(key, 6)}{summary[key]}")
            lines.append("```")

    return "\n".join(lines)


# ----------------------------------------------------------------------
# Pre-match preview — match_info + ground_stats
# ----------------------------------------------------------------------


def format_match_preview(info, ground=None):
    """
    Render a pre-match preview from toss, venue, captains and venue history.

    Args:
        info: match_info() response
        ground: Optional ground_stats() response

    Returns:
        Formatted string
    """
    lines = ["🏏 MATCH PREVIEW"]

    venue = info.get("venue") or {}
    venue_name = venue.get("name") or venue.get("longName")
    if venue_name:
        lines.append(venue_name)

    time = info.get("time") or {}
    if time.get("startDate"):
        lines.append(str(time["startDate"]))

    toss = info.get("toss") or {}
    if toss.get("winner_team"):
        decision = toss.get("decision") or "?"
        lines.append(f"Toss: {toss['winner_team']} chose to {decision}")

    captains = info.get("captains") or []
    if captains:
        lines.append("")
        lines.append("Captains:")
        for c in captains:
            if c.get("name"):
                lines.append(f"- {c['name']} ({c.get('team_name', '?')})")

    if ground:
        summary = (ground or {}).get("summary") or {}
        if summary:
            lines.append("")
            lines.append("At this venue:")
            lines.append("```")
            for key in ("Mat", "Won", "Lost", "Drawn", "Ave", "RPO"):
                if key in summary:
                    lines.append(f"{_fit(key, 7)}{summary[key]}")
            lines.append("```")

    return "\n".join(lines)


# ----------------------------------------------------------------------
# Fetch helpers
# ----------------------------------------------------------------------


def match_slugs(m):
    """
    Build the (series_slug, match_slug) pair every match call needs.

    This construction is repeated throughout the cricdata README and the
    original notebook; centralize it here.

    Args:
        m: A match object from a fixtures/series listing

    Returns:
        Tuple of (series_slug, match_slug)
    """
    series = m.get("series") or {}
    return (
        f"{series.get('slug')}-{series.get('objectId')}",
        f"{m.get('slug')}-{m.get('objectId')}",
    )


def fetch_match_bundle(ci, series_slug, match_slug):
    """
    Fetch everything the scorecard views need.

    cricdata caches the scorecard page per (series_slug, match_slug), so these
    five calls resolve to a single HTTP request.

    Args:
        ci: CricinfoClient
        series_slug: Series slug
        match_slug: Match slug

    Returns:
        Dict with info, scorecard, overs, partnerships, fall_of_wickets
    """
    return {
        "info": ci.match_info(series_slug, match_slug),
        "scorecard": ci.match_scorecard(series_slug, match_slug),
        "overs": ci.match_overs(series_slug, match_slug),
        "partnerships": ci.match_partnerships(series_slug, match_slug),
        "fall_of_wickets": ci.match_fall_of_wickets(series_slug, match_slug),
    }


async def fetch_many_scorecards(aci, matches, concurrency=15):
    """
    Fetch scorecards for many matches concurrently.

    Args:
        aci: AsyncCricinfoClient
        matches: Match objects from a series listing
        concurrency: Maximum in-flight requests

    Returns:
        List of (match, scorecard_or_None) pairs, in input order. A failed
        fetch yields None rather than aborting the batch.
    """
    import asyncio

    sem = asyncio.Semaphore(concurrency)

    async def one(m):
        series_slug, slug = match_slugs(m)
        async with sem:
            try:
                return await aci.match_scorecard(series_slug, slug)
            except Exception:
                return None

    results = await asyncio.gather(*(one(m) for m in matches))
    return list(zip(matches, results))
