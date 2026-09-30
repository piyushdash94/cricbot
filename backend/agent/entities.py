"""Deterministic cricket entity resolution for Pandit's LangGraph state.

This deliberately runs before tool routing.  A base completion model is useful
for phrasing, but team typos, temporal phrases, and list intent are safer to
resolve deterministically so every downstream node sees the same query.
"""

from __future__ import annotations

from datetime import datetime
from difflib import SequenceMatcher
import re
from typing import Any


TEAM_ALIASES: dict[str, tuple[str, ...]] = {
    "RCB": ("rcb", "royal challengers bengaluru", "royal challengers bangalore"),
    "CSK": ("csk", "chennai super kings"),
    "MI": ("mi", "mumbai indians"),
    "KKR": ("kkr", "kolkata knight riders"),
    "SRH": ("srh", "sunrisers hyderabad"),
    "PBKS": ("pbks", "punjab kings", "kings xi punjab", "kxip"),
    "RR": ("rr", "rajasthan royals"),
    "DC": ("dc", "delhi capitals", "delhi daredevils"),
    "GT": ("gt", "gujarat titans"),
    "LSG": ("lsg", "lucknow super giants"),
}

PLAYER_ALIASES: dict[str, tuple[str, ...]] = {
    "Virat Kohli": ("virat kohli", "kohli"),
    "Rohit Sharma": ("rohit sharma", "rohit"),
    "MS Dhoni": ("ms dhoni", "dhoni"),
    "Shubman Gill": ("shubman gill", "gill"),
    "Jasprit Bumrah": ("jasprit bumrah", "bumrah"),
    "Rishabh Pant": ("rishabh pant", "pant"),
    "KL Rahul": ("kl rahul", "rahul"),
    "Hardik Pandya": ("hardik pandya", "hardik"),
    "Krunal Pandya": ("krunal pandya", "krunal"),
    "Shreyas Iyer": ("shreyas iyer", "iyer"),
    "Phil Salt": ("phil salt", "salt"),
    "Josh Hazlewood": ("josh hazlewood", "hazlewood"),
}

MATCH_TERMS = (
    "final", "qualifier", "eliminator", "playoff", "semi-final", "semifinal",
    "opener", "opening match", "league match", "live match",
)
UNIQUE_MATCH_TERMS = {"final", "qualifier", "eliminator", "semi-final", "semifinal", "opener", "opening match"}

TOPICS = (
    "analytics", "analysis", "impact", "momentum", "pitch", "conditions",
    "probability", "swing", "standings", "points table", "rank", "nrr",
    "player", "batter", "batting", "bowler", "bowling", "runs", "wicket",
    "scorecard", "commentary", "ball by ball", "ball-by-ball", "partnership",
)

VENUES: dict[str, tuple[str, ...]] = {
    "Wankhede Stadium": ("wankhede",),
    "Eden Gardens": ("eden gardens", "eden"),
    "M. Chinnaswamy Stadium": ("chinnaswamy", "bengaluru", "bangalore"),
    "MA Chidambaram Stadium": ("chepauk", "chidambaram", "chennai"),
    "Narendra Modi Stadium": ("narendra modi stadium", "ahmedabad"),
}

FUZZY_TEAM_BLOCKLIST = {
    "all", "and", "are", "can", "did", "for", "get", "had", "has", "last",
    "list", "match", "matches", "not", "see", "show", "the", "this", "was",
    "what", "when", "where", "who", "won", "year", "you",
}


def _contains_phrase(text: str, phrase: str) -> bool:
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text))


def _exact_entities(text: str, aliases: dict[str, tuple[str, ...]]) -> list[str]:
    return [canonical for canonical, names in aliases.items() if any(_contains_phrase(text, name) for name in names)]


def _team_entities(text: str) -> tuple[list[str], list[dict[str, str]]]:
    teams = _exact_entities(text, TEAM_ALIASES)
    corrections: list[dict[str, str]] = []
    abbreviations = tuple(TEAM_ALIASES)
    for token in re.findall(r"\b[a-z]{3,4}\b", text):
        if token in FUZZY_TEAM_BLOCKLIST or any(token in aliases for aliases in TEAM_ALIASES.values()):
            continue
        candidate = max(abbreviations, key=lambda abbreviation: SequenceMatcher(None, token.upper(), abbreviation).ratio())
        confidence = SequenceMatcher(None, token.upper(), candidate).ratio()
        if confidence >= 0.66 and candidate not in teams:
            teams.append(candidate)
            corrections.append({"from": token.upper(), "to": candidate, "type": "team_typo"})
    return teams, corrections


def _temporal_entities(text: str, current_year: int) -> tuple[list[int], str | None, list[dict[str, str]]]:
    years = [int(value) for value in re.findall(r"\b20\d{2}\b", text)]
    reference = None
    corrections: list[dict[str, str]] = []
    temporal_patterns = (
        (("last year", "previous year"), current_year - 1, "last year"),
        (("this year", "current year"), current_year, "this year"),
        (("two years ago",), current_year - 2, "two years ago"),
    )
    for phrases, resolved_year, label in temporal_patterns:
        matched = next((phrase for phrase in phrases if _contains_phrase(text, phrase)), None)
        if matched:
            reference = label
            if resolved_year not in years:
                years.append(resolved_year)
            corrections.append({"from": matched, "to": str(resolved_year), "type": "temporal"})
            break
    return sorted(set(years), reverse=True), reference, corrections


def _intent(text: str, teams: list[str], players: list[str]) -> tuple[str, bool]:
    wants_all = bool(re.search(r"\b(all|every|complete|full list)\b", text))
    list_verb = bool(re.search(r"\b(list|show|find|get|give)\b", text))
    match_word = bool(re.search(r"\b(match|matches|fixtures|games|results)\b", text))
    if teams and (wants_all or list_verb) and match_word:
        return "list_matches", wants_all
    if teams and match_word:
        return "search_matches", wants_all
    if players:
        return "player_lookup", wants_all
    if "standing" in text or "points table" in text:
        return "standings", wants_all
    return "question", wants_all


def resolve_entities(
    message: str,
    *,
    history: list[dict[str, str]] | None = None,
    ui_context: dict[str, Any] | None = None,
    current_year: int | None = None,
) -> dict[str, Any]:
    """Extract and resolve the entities that should guide later graph nodes."""
    text = message.casefold().strip()
    year = current_year or datetime.now().year
    teams, corrections = _team_entities(text)
    players = _exact_entities(text, PLAYER_ALIASES)

    # Carry a previously discussed entity only when the new message omitted it
    # and does not already name a single fixture ("the 2024 final").
    terms = [term for term in MATCH_TERMS if _contains_phrase(text, term)]
    venues = _exact_entities(text, VENUES)
    self_contained = bool(teams or players or venues) or any(term in UNIQUE_MATCH_TERMS for term in terms)
    if not self_contained:
        for item in reversed(history or []):
            prior_teams, _ = _team_entities(item.get("content", "").casefold())
            if prior_teams:
                teams = prior_teams
                break
    if not self_contained:
        for item in reversed(history or []):
            prior_players = _exact_entities(item.get("content", "").casefold(), PLAYER_ALIASES)
            if prior_players:
                players = prior_players
                break

    years, temporal_reference, temporal_corrections = _temporal_entities(text, year)
    corrections.extend(temporal_corrections)
    topics = [topic for topic in TOPICS if _contains_phrase(text, topic)]
    intent, wants_all = _intent(text, teams, players)

    corrected_query = message.strip()
    for correction in corrections:
        corrected_query = re.sub(
            rf"(?i)(?<![a-z0-9]){re.escape(correction['from'])}(?![a-z0-9])",
            correction["to"],
            corrected_query,
        )

    retrieval_parts: list[str] = [*teams, *(str(value) for value in years), *players, *terms, *venues, *topics]
    if not retrieval_parts:
        retrieval_parts = [corrected_query]
    retrieval_query = " ".join(dict.fromkeys(retrieval_parts))

    return {
        "teams": teams,
        "players": players,
        "years": years,
        "match_terms": terms,
        "topics": topics,
        "venues": venues,
        "intent": intent,
        "wants_all": wants_all,
        "temporal_reference": temporal_reference,
        "corrections": corrections,
        "corrected_query": corrected_query,
        "retrieval_query": retrieval_query,
        "ui_season": (ui_context or {}).get("season"),
    }
