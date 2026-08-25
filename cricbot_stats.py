"""
Access layer for cricdata's Statsguru backend.

Statsguru is scraped from HTML tables, so every value arrives as a string —
including "-" for missing data and "254*" for a not-out score. Nothing
downstream can do arithmetic until that is normalized, which is what this
module is for.

It is also slower and more brittle than the SSR scorecard endpoints, so
callers should treat a failure here as degraded output rather than an error.
"""

from scorecard_helpers import get_nested


def to_number(value):
    """
    Coerce a Statsguru cell to a number.

    Handles the annotations Statsguru embeds in its values: '*' marks a
    not-out score, '+' appears on some aggregate rows, and '-' means the
    stat does not apply.

    Args:
        value: Raw cell value (usually a string)

    Returns:
        int, float, or None when the value is missing or unparseable
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value

    text = str(value).strip().replace("*", "").replace("+", "")
    text = text.replace(",", "")

    if not text or text == "-":
        return None

    try:
        return int(text)
    except ValueError:
        pass

    try:
        return float(text)
    except ValueError:
        return None


def numeric_rows(rows, keys=None):
    """
    Coerce numeric columns across a list of Statsguru rows.

    Column names vary by format and stat type, so unknown keys are left
    untouched rather than dropped.

    Args:
        rows: List of dicts from a Statsguru detail table
        keys: Column names to coerce; None means try every column

    Returns:
        New list of dicts with the named columns converted where possible
    """
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue

        converted = {}
        for key, value in row.items():
            if keys is not None and key not in keys:
                converted[key] = value
                continue

            number = to_number(value)
            converted[key] = number if number is not None else value

        out.append(converted)

    return out


def ground_id_from_match(scorecard):
    """
    Extract the Statsguru ground id from a scorecard.

    Statsguru URLs use ground.id (292 for Eden Gardens). The sibling
    ground.objectId (57980) is the Cricinfo object id and will not resolve.

    Args:
        scorecard: match_scorecard() response

    Returns:
        Ground id, or None when absent
    """
    ground_id = get_nested(scorecard, ["match", "ground", "id"], None)
    return ground_id if isinstance(ground_id, int) else None


def ground_name_from_match(scorecard):
    """
    Extract the venue name from a scorecard.

    Args:
        scorecard: match_scorecard() response

    Returns:
        Venue name, or None
    """
    return (
        get_nested(scorecard, ["match", "ground", "name"], None)
        or get_nested(scorecard, ["match", "ground", "longName"], None)
    )


def resolve_player(ci, name, limit=5):
    """
    Look up a player id by name.

    Args:
        ci: CricinfoClient
        name: Search string
        limit: Candidates to consider

    Returns:
        Dict with id, display_name and the full candidate list, or None when
        nothing matched. The candidates are returned so an ambiguous search
        can be surfaced to the user rather than silently taking the first.
    """
    results = ci.search_players(name, limit=limit) or []
    if not results:
        return None

    first = results[0]
    return {
        "id": first.get("id"),
        "display_name": first.get("displayName") or first.get("shortName"),
        "candidates": [
            {"id": r.get("id"), "name": r.get("displayName")} for r in results
        ],
        "ambiguous": len(results) > 1,
    }


def safe_call(fn, *args, **kwargs):
    """
    Run a Statsguru call, returning None instead of raising.

    Statsguru parses HTML with regex and is the most fragile part of the
    stack. A venue lookup failing must never take down a scorecard render.

    Args:
        fn: Callable to invoke
        *args, **kwargs: Passed through

    Returns:
        The call's result, or None if it raised
    """
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None
