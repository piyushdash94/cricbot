# Local match archive (DuckDB)

Ball-by-ball data for every finished IPL T20 match, in one local DuckDB file, with an
evaluation that reconciles it against the official scorecard and a stats query CLI.
Run every command from the repository root.

Every finished IPL T20 match can be stored in one local file, `data/cricbot.duckdb`
(override with `CRICBOT_DB`), queryable with SQL across every season.

```bash
python -m backend.db.ingest --seasons 2025          # one season (~3 min)
python -m backend.db.ingest --seasons all           # every IPL edition, 2008 onwards (~1 hour)
python -m backend.db.evaluate --season 2025         # check the stored data
```

Re-running is safe: stored matches are skipped, each match is written atomically, and
failed or partial matches are retried. Keep `--workers` at 2–3; more parallel requests
trigger upstream 502s.

| Table / view | Contents |
|---|---|
| `matches` | One row per match: season, teams, venue, result, paths for cricdata |
| `balls` | One row per delivery incl. wides and no-balls: batter, bowler, runs split into bat/wides/no-balls/byes/leg byes, wicket and dismissal type, commentary text |
| `ref_innings`, `ref_batting`, `ref_bowling` | The official Cricinfo scorecard (dismissal text, and the answer key for evaluation) |
| `innings_card`, `batting_card`, `bowling_card` | Scorecards rebuilt from `balls` |
| `ingest_log` | Status per match: `ok`, `partial` (fewer items than the feed's own count), `no_balls`, `error` |

```python
import duckdb
con = duckdb.connect("data/cricbot.duckdb", read_only=True)
con.sql("""
    SELECT bowler, count(*) FILTER (WHERE is_legal) AS balls,
           sum(runs_total - bye_runs - legbye_runs) AS conceded
    FROM balls JOIN matches USING (match_id)
    WHERE season = '2025' AND over_number >= 17
    GROUP BY bowler HAVING balls >= 60 ORDER BY conceded * 6.0 / balls LIMIT 5
""").show()                                         # best death-over economy, IPL 2025
```

**Querying.** `backend.db.queries` has ready-made stats commands (super overs excluded,
official scoring conventions, modern team codes such as PBKS for Kings XI Punjab):

```bash
python -m backend.db.queries seasons                       # which years are ingested + scoring trends
python -m backend.db.queries top-batters --year 2025       # or --year 2016-2018, or omit for all time
python -m backend.db.queries top-bowlers --year 2025
python -m backend.db.queries player "virat kohli"          # by year and by phase
python -m backend.db.queries phase death --role bowl --year 2025
python -m backend.db.queries records --year 2025           # totals, scores, figures, fastest 50s, sixes
python -m backend.db.queries matches --year 2016 --team RCB
python -m backend.db.queries match 981019                  # card rebuilt from the balls
python -m backend.db.queries h2h MI CSK
python -m backend.db.queries commentary 981019 --search six
python -m backend.db.queries sql "SELECT phase, round(6.0*sum(runs_total)/count(*) FILTER (WHERE is_legal),2) AS rr FROM d WHERE year = 2025 GROUP BY phase"
```

Every command takes `--limit N` and `--csv file.csv`. Checked against published season
leaders (2008-2010, 2025): Marsh 616 / Tanvir 22, Hayden 572 / RP Singh 23, Tendulkar 618 /
Ojha 21, Sai Sudharsan 759 / Prasidh Krishna 25.

**Evaluation.** `backend.db.evaluate` rebuilds every innings total, batting line and
bowling figure from `balls` and compares it with the official card. Two independent
sources agreeing is the evidence that the ball data is complete and the scoring rules
are right. Rules learned this way, all covered by `backend/tests/test_db.py`:

- Extras come from each ball's `over.wide/noBall/byes/legByes` run counts, not `scoreValue`:
  ESPN reports "(no ball) 4 byes" with `scoreValue = 1`.
- "Retired not out (hurt)" is flagged as a dismissal upstream but is not a wicket; "retired out" is.
- Empty placeholder items (over 0, ball 0, no text) are not deliveries.
- A batter dismissed without facing (non-striker run out) is a correct `0 (0)`.
- Umpire miscounts are real: some overs have 7 (or 5) legal balls. The feed records every delivery;
  the card rounds to whole overs. `evaluate` reports these as *explained* when runs, wickets and every
  batter's balls faced still match, and flags everything else as *UNEXPLAINED*.

DuckDB allows one writer per file: run `ingest` and `queries`/`evaluate` one at a time.

**Partial feeds.** About 3% of matches (most in 2008 and 2013) come back with one to three
items fewer than ESPN's own `count` field. Fetching page by page returns the same items, and
every partial checked so far reconciles exactly with the official scorecard: the counter is
off, no deliveries are missing. `ingest` logs them as `partial` and `evaluate` prints how
many reconcile.

## Modules

| Module | Responsibility |
|---|---|
| `parse.py` | Pure parsing of ESPN play-by-play and the Cricinfo scorecard into flat rows |
| `store.py` | DuckDB schema, scorecard views, atomic per-match writes |
| `ingest.py` | Season discovery, one request per match ball feed, retries, idempotent ingest CLI |
| `evaluate.py` | Rebuilds scorecards from balls and reconciles them with the official card |
| `queries.py` | Stats CLI: seasons, leaders, players, phases, match cards, records, head to head, commentary, SQL |
| `read.py` | Returns a stored match in the API's match-detail shape (not wired into the API yet) |

Tests: `python -m pytest backend/tests/test_db.py` (offline, uses a recorded SRH v RR fixture).

Data comes from ESPNcricinfo through the unaffiliated `cricdata` package; keep the database
local and review the provider's terms before any public use.
