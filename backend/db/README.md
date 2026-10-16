# Local match archive (DuckDB)

Ball-by-ball data for every IPL T20 match in one local file, `data/cricbot.duckdb`
(override with `CRICBOT_DB`). It powers the stats CLI, the `/api/stats/*` endpoints and the
Stats view in the UI. Run every command from the repository root.

## Quick start (about 20 seconds)

```bash
python -m backend.db.cricsheet_load --sync     # download Cricsheet and load every IPL match
python -m backend.db.queries top-batters --year 2016
```

That is all the stats need. [Cricsheet](https://cricsheet.org) (Open Data Commons Attribution
licence) has every delivery of every IPL match since 2008. Its player register maps Cricsheet
ids to Cricinfo ids, so a player has the same `player_id` whichever source a ball came from.

## Optional: ESPN for commentary and the official card

```bash
python -m backend.db.ingest --seasons 2025          # one season (~3 min)
python -m backend.db.ingest --seasons all           # every edition (~1 hour)
```

The ESPN play-by-play feed (via `cricdata`) adds what Cricsheet does not have: commentary
text, full player names (Virat Kohli rather than V Kohli), the official Cricinfo scorecard
used as the answer key by `evaluate`, and matches too recent for Cricsheet. Re-running is
safe: stored matches are skipped and each match is written atomically. Keep `--workers`
at 2–3; more parallel requests trigger upstream 502s.

## Tables and views

| Table / view | Contents |
|---|---|
| `matches` | One row per match: season, teams, venue, result (ESPN fixture list; Cricsheet fills gaps) |
| `cricsheet_balls` | One row per delivery from Cricsheet |
| `balls` | One row per delivery from ESPN, with commentary text |
| `deliveries` | Every delivery, one source per match: Cricsheet when loaded, otherwise ESPN |
| `ref_innings`, `ref_batting`, `ref_bowling` | The official Cricinfo scorecard (from the ESPN ingest) |
| `players` | Display names per `player_id` from Cricsheet |
| `innings_card`, `batting_card`, `bowling_card` | Scorecards rebuilt from `balls`; `cs_*` the same from `cricsheet_balls` |
| `ingest_log` | ESPN ingest status per match: `ok`, `partial`, `no_balls`, `error` |

Each delivery row has batter, bowler, runs split into bat / wides / no-balls / byes / leg
byes, wicket and dismissal type, and a one-line description.

## Querying

`backend.db.queries` has ready-made stats commands. They exclude super overs, follow
official scoring conventions and use current team codes (PBKS for Kings XI Punjab, DC for
Delhi Daredevils and Capitals, DCH for Deccan Chargers).

```bash
python -m backend.db.queries seasons                       # which years are loaded + scoring trends
python -m backend.db.queries top-batters --year 2025       # or --year 2016-2018, or omit for all time
python -m backend.db.queries top-bowlers --year 2025
python -m backend.db.queries players bumrah                # find a player_id
python -m backend.db.queries player "virat kohli"          # by year and by phase
python -m backend.db.queries phase death --role bowl --year 2024
python -m backend.db.queries records --year 2025           # totals, scores, figures, fastest 50s, sixes
python -m backend.db.queries matches --year 2016 --team RCB
python -m backend.db.queries match 981019                  # card rebuilt from the balls
python -m backend.db.queries h2h MI CSK
python -m backend.db.queries commentary 981019 --search six   # needs the ESPN ingest for text
python -m backend.db.queries sql "SELECT phase, count(*) FILTER (WHERE is_six) AS sixes FROM d GROUP BY phase"
```

Every command takes `--limit N` and `--csv file.csv`. The same commands are served as JSON
by `backend/apis/stats.py` (`/api/stats/batting`, `/bowling`, `/records`, `/phase`,
`/players`, `/players/{id}`, `/h2h`, `/seasons`, `/status`).

**Checked against published records:** the top run-scorer and top wicket-taker of every
season from 2008 to 2025 (36 of 36) match, e.g. Kohli 973 in 2016, Gill 890 in 2023,
Bravo 32 in 2013, Prasidh Krishna 25 in 2025.

## Evaluation

```bash
python -m backend.db.evaluate                       # ESPN balls vs the official card
python -m backend.db.evaluate --source cricsheet    # Cricsheet balls vs the official card
```

`evaluate` rebuilds every innings total, batting line and bowling figure from one ball
source and compares it with the official card. On 248 matches checked so far (2008–2010
and 2025), the sources disagree with the card in different places, so any single error
is outvoted by the other two:

| Source vs official card | Matches fully correct | Explained | Unexplained |
|---|---|---|---|
| Cricsheet | 241 / 248 | 6 umpire miscounts | 1 |
| ESPN | 240 / 248 | the same 6 | 2 |

Rules learned this way, each covered by a test:

- **ESPN:** extras come from each ball's `over.wide/noBall/byes/legByes` run counts, not
  `scoreValue`, which reports "(no ball) 4 byes" as 1 run.
- **Cricsheet:** older files record "no ball + 4 byes" as 5 no-ball runs (the pre-2017 Laws).
  The official card charges the bowler 1 and records the rest as byes, so the loader does too.
- **Both:** "retired not out (hurt)" is not a wicket; "retired out" is but is not the bowler's.
- **Both:** a batter dismissed without facing (non-striker run out) is a correct `0 (0)`.
- **Umpire miscounts are real:** some overs have 7 (or 5) legal balls. The balls record every
  delivery while the card rounds to whole overs. `evaluate` marks these *explained* when
  runs, wickets and every batter's balls faced still match; everything else is *UNEXPLAINED*.
- **ESPN partial feeds:** about 3% of matches come back one to three items short of ESPN's own
  `count`. Every partial checked reconciles exactly: the counter is off, no balls are missing.

DuckDB allows one writer per file: run the loaders and `queries`/`evaluate` one at a time.

## Modules

| Module | Responsibility |
|---|---|
| `cricsheet_load.py` | Loads Cricsheet deliveries and the player register (~20 s for every season) |
| `ingest.py` | Optional ESPN ingest: commentary, official card, one request per match, retries |
| `parse.py` | Pure parsing of ESPN play-by-play and the Cricinfo scorecard into rows |
| `store.py` | DuckDB schema and views |
| `evaluate.py` | Rebuilds scorecards from either source and reconciles them with the official card |
| `queries.py` | Stats commands, used by the CLI and by `backend/apis/stats.py` |
| `read.py` | Returns a stored match in the API's match-detail shape (not wired into the API yet) |

Tests: `python -m pytest backend/tests/test_db.py backend/tests/test_cricsheet_db.py`
(offline; recorded SRH v RR fixtures from both sources).

Keep the database local. Cricsheet data needs attribution if shared; ESPN data comes through
the unaffiliated `cricdata` package, so review the provider's terms before any public use.
