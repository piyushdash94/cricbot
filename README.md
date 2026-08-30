# cricbot

Compact cricket match summaries and text-mode charts, built on
[cricdata](https://github.com/arnavbonigala/cricdata) (ESPNCricinfo).

Turns a match into something that fits in a single chat message:

```
🏏 MATCH
KKR vs RCB
RCB won by 7 wickets (with 22 balls remaining)
Eden Gardens, Kolkata

KKR: 174/8 in 20 overs
Extras: 6 | 4s: 18 | 6s: 8

Top batters:
- Ajinkya Rahane: 56(31) 4s:6 6s:4 SR:180.64
- Sunil Narine: 44(26) 4s:5 6s:3 SR:169.23

Phases:
Powerplay    1-6   60/1  RPO 10.0
Middle      7-16   91/5  RPO 9.1
Death      17-20   23/2  RPO 5.8

Runs/over (1-20):
▂▁▂▆▆▇▃▄█▅▂▅▃▄▂▃▂▄▂▃
W········WW·W·WW··WW

Top stands:
Narine/Rahane   ███████████▓▓▓▓▓▓▓▓▓▓▓▓▓ 103(55)

Win% KKR (whole match):
▅▄▄▄▅▆▅▅▆▆▅▆▆▆▅▅▄▄▄▄▄▄▃▂▁▁▁▁▂▂▁▂▁▁▁▁▁
```

All charts are block characters — no image pipeline, no plotting library.
They render anywhere a monospace code block does.

## Status

This is a **formatting toolkit plus an exploratory notebook**, not yet a
running bot. Every formatter takes an already-fetched payload and returns a
string, so wiring it to Discord, Telegram, or a CLI is the remaining step —
there is no transport, no command router, and no hosting config in here yet.

## Install

Python 3.10+ (required by `cricdata`).

```bash
pip install -r requirements.txt
```

`beautifulsoup4` is imported lazily and is only needed for commentary
parsing — the scorecard path works without it.

For rendered PNG charts (as opposed to the block-character ones, which need
nothing), install the optional extra:

```bash
pip install -r requirements-plots.txt      # matplotlib
```

Everything else works without it; `scorecard_plots.PLOTS_AVAILABLE` reports
whether it is present.

## Quick start

```python
from cricdata import CricinfoClient
from scorecard_renderer import build_compact_scorecard
from cricbot_views import match_slugs

ci = CricinfoClient()

fixtures = ci.series_fixtures("ipl-2025-1449924")
match = fixtures["content"]["matches"][0]
series_slug, match_slug = match_slugs(match)

info = ci.match_info(series_slug, match_slug)
scorecard = ci.match_scorecard(series_slug, match_slug)

print(build_compact_scorecard(info, scorecard))
```

### Turning on the extras

```python
build_compact_scorecard(
    info, scorecard,
    top_n=3,            # batters/bowlers per innings
    max_chars=1900,     # output is truncated to fit
    phases=True,        # powerplay / middle / death splits
    charts=True,        # runs-per-over, win probability, run worm
    partnerships=True,  # biggest stands, split by contribution
    fow=False,          # fall of wickets (on by default)
)
```

Approximate sizes for a completed T20:

| Configuration | Chars |
|---|---|
| Default (batters, bowlers, FOW) | ~1480 |
| `phases + charts + partnerships`, `fow=False` | ~1630 |
| Everything on | ~2320 (exceeds one Discord message) |

FOW is the expensive section. If you need charts *and* FOW, either split by
innings or raise `max_chars`.

## Modules

| Module | Purpose |
|---|---|
| `scorecard_helpers.py` | Data extraction — player names, validation, match metadata, fall of wickets, innings analytics |
| `scorecard_charts.py` | Block-character charts (stdlib only) |
| `scorecard_renderer.py` | `build_compact_scorecard()` — the main entry point |
| `cricbot_views.py` | Live scores, standings, player cards, match previews, fetch helpers |
| `cricbot_stats.py` | Statsguru access layer — numeric coercion, id resolution |
| `cricbot_venue.py` | Venue profiling and pitch reads from `ground_stats()` |
| `impact_lab.py` | Experimental: momentum, impact scorecard, pitch profile |
| `scorecard_plots.py` | Optional PNG charts (needs the `matplotlib` extra) |
| `Criketmatchbot.ipynb` | Exploratory notebook (API shapes, commentary) |
| `Feature_Lab.ipynb` | Experiments for the `impact_lab` analytics |
| `tests/` | Offline test suite |

### Charts

```python
from scorecard_charts import (
    sparkline,          # values -> single-line block chart
    hbar,               # one horizontal bar
    manhattan,          # runs per over + wicket markers
    win_probability,    # win% curve on a fixed 0-100 scale
    worm,               # cumulative runs, innings compared
    partnership_bars,   # stands split by each batter's share
    phase_table,        # powerplay / middle / death
)
```

Every chart returns a placeholder string rather than raising when given no
data, so a missing section never breaks a message.

### Plots (optional)

```python
from scorecard_plots import render_match_report, PLOTS_AVAILABLE

if PLOTS_AVAILABLE:
    paths = render_match_report(scorecard, "out/")
```

Renders win probability, manhattan, worm, partnerships and momentum as PNGs,
reusing the same extractors as the text charts. Headless (`Agg` backend), and
every figure is closed after writing so a long-running bot does not leak them.

### Views

```python
from cricbot_views import (
    format_live_matches,    # ci.live_matches()
    format_standings,       # ci.series_standings()
    format_player_card,     # ci.player_bio() + ci.player_career_stats()
    format_match_preview,   # ci.match_info() + ci.ground_stats()
    fetch_match_bundle,     # info + scorecard + overs + partnerships + FOW
    fetch_many_scorecards,  # async, bounded concurrency
)
```

## Data notes

Things that are easy to get wrong, learned from real payloads:

**Win probability is the batting side's.** Cricinfo reports
`winProbability` for whichever team is batting in that innings, not for a
fixed team. Verified on a completed match: the chasing innings ends at 100,
while the side that lost ends its own innings at 42. To follow one team
across a whole match you must invert the innings where they bowled —
`win_prob_series()` does this.

**Optional sections are free.** `cricdata` caches the scorecard page per
`(series_slug, match_slug)`, so `match_scorecard`, `match_info`,
`match_overs`, `match_partnerships` and `match_fall_of_wickets` all resolve
to a single HTTP request. Phases, charts and partnerships add no network
cost. The cache never evicts, so cap it in a long-running process.

**Field names differ from the obvious guess.** Standings rows use
`teamInfo` and `nrr` — not `team` and `netRunRate`. Bowler runs conceded is
`conceded`, not `runs`. Batting strike rate is `strikerate`, lowercase `r`.

**Dismissal text is sometimes a dict.** `dismissalText` arrives as either a
string or `{short, long, commentary}`; `dismissal_text()` normalizes it.

**Statsguru returns everything as strings.** Every value from the
`ground_stats` / `player_*` family arrives as text — `"46.85"`, `"254*"` for a
not-out, `"-"` for missing. `cricbot_stats.to_number()` normalizes them.
Column names also vary by format and stat type, so look fields up through a
list of candidates rather than one hard-coded key.

**The async client does not cache.** The sync `SSR` caches the scorecard page
per `(series_slug, match_slug)`, which is why `fetch_match_bundle()`'s five
calls cost one request. `AsyncSSR` has no cache, so the same pattern async
would cost five. Use `fetch_many_scorecards()` for concurrency across
matches, where each match is one request either way.

**No full-innings wagon wheel.** `wagonX/Y/Zone`, `pitchLine` and
`shotControl` exist only on the SSR ball feed, which populates ball-level
detail for the most recent over of each innings only. `match_ball_by_ball()`
routes to ESPN instead — full match, but no shot coordinates. A live
last-over shot map is possible; a full wagon wheel is not.

## Tests

The suite runs offline against synthetic payloads shaped like real
ESPNCricinfo responses — no network, no API keys:

```bash
pytest                          # or, with no pytest installed:
python tests/test_scorecard.py
python tests/test_venue.py
python tests/test_plots.py      # render tests skip without matplotlib
```

It covers the failure modes that actually bit during development: dismissal
text arriving as a dict, bowler runs missing the `conceded` key, substitutes
leaking into output, truncation splitting a code fence, and the impact
zero-sum invariant holding as `wicket_runs` varies.

## Notebooks

- **`Criketmatchbot.ipynb`** — exploration of the API response shapes, now
  importing from these modules rather than redefining renderers inline.
- **`Feature_Lab.ipynb`** — experiments for `impact_lab.py`: turning points,
  momentum tuning, impact calibration, pitch profiling.

Both carry stored `pprint` output and are large. Clearing outputs before
committing keeps the repo small.

## Attribution

Data comes from ESPNCricinfo via `cricdata`, which is unaffiliated with
ESPN. Check their terms before deploying anything public-facing.
