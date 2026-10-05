"""Canonical IPL franchise names, codes, and colours across every season.

Sources name a franchise as it was called at the time (Delhi Daredevils, Kings
XI Punjab, Royal Challengers Bangalore). These helpers give every era's name a
stable code so archive, scorecard, and chat agree.
"""

from __future__ import annotations

import re

FRANCHISES: dict[str, dict[str, str]] = {
    "chennai super kings": {"abbr": "CSK", "color": "#f2c200"},
    "mumbai indians": {"abbr": "MI", "color": "#1f6fc4"},
    "royal challengers bengaluru": {"abbr": "RCB", "color": "#e43f49"},
    "royal challengers bangalore": {"abbr": "RCB", "color": "#e43f49"},
    "kolkata knight riders": {"abbr": "KKR", "color": "#5b3a9b"},
    "sunrisers hyderabad": {"abbr": "SRH", "color": "#f26522"},
    "deccan chargers": {"abbr": "DCH", "color": "#8a8f98"},
    "punjab kings": {"abbr": "PBKS", "color": "#d64b6a"},
    "kings xi punjab": {"abbr": "PBKS", "color": "#d64b6a"},
    "rajasthan royals": {"abbr": "RR", "color": "#e7368f"},
    "delhi capitals": {"abbr": "DC", "color": "#2f5fb3"},
    "delhi daredevils": {"abbr": "DC", "color": "#2f5fb3"},
    "gujarat titans": {"abbr": "GT", "color": "#4a6fa5"},
    "gujarat lions": {"abbr": "GL", "color": "#e8792b"},
    "lucknow super giants": {"abbr": "LSG", "color": "#2b9bd8"},
    "pune warriors": {"abbr": "PWI", "color": "#3fa9d6"},
    "pune warriors india": {"abbr": "PWI", "color": "#3fa9d6"},
    "rising pune supergiant": {"abbr": "RPS", "color": "#7b4fa3"},
    "rising pune supergiants": {"abbr": "RPS", "color": "#7b4fa3"},
    "kochi tuskers kerala": {"abbr": "KTK", "color": "#e08a2e"},
}


def _key(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").casefold()).strip()


def team_code(name: str) -> str:
    """Stable franchise code; unknown names fall back to their initials."""
    known = FRANCHISES.get(_key(name))
    if known:
        return known["abbr"]
    initials = "".join(word[0] for word in re.findall(r"[A-Za-z]+", name or ""))
    return initials.upper()[:4] or "TBD"


def team_color(name: str) -> str:
    return FRANCHISES.get(_key(name), {}).get("color", "#7c8a82")


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").casefold()).strip("-")
