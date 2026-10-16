# Archived branch: PNG charts and venue profiling (August 2026)

This branch preserves two commits that were added after PR #1 merged and never reached `main`:

| Commit | What it adds |
|---|---|
| `6ce19c1` | Venue profiling from cricdata's Statsguru backend (`cricbot_venue.py`, `cricbot_stats.py`, `tests/test_venue.py`) |
| `56867a8` | Optional PNG charts with matplotlib: win probability, manhattan, worm, partnerships, momentum (`scorecard_plots.py`, `tests/test_plots.py`, `requirements-plots.txt`) |

**Do not merge this branch as is.** It uses the repository's old flat layout (modules at the root).
`main` has since moved everything under `backend/` (`backend/src/`, `backend/tests/`), so a merge
would put these files back in the wrong place.

To reuse the work, port the modules into the new layout in a fresh branch from `main`, for example
`cricbot_venue.py` to `backend/src/venue.py` and `scorecard_plots.py` to `backend/src/scorecard_plots.py`,
fixing imports to `backend.src.*`.

It replaces the branch `claude/scorecard-method-improvements-ov1ivl` (same commits).
