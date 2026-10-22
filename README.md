# Cricbot

A local-first IPL archive and cricket assistant that turns ESPNcricinfo-shaped data into searchable match pages, scorecards, delivery timelines, analytics, and natural-language UI actions.

The repository now has two clear surfaces:

```text
cricbot/
├── backend/
│   ├── agent/         # Pandit orchestration, stats planner, LLM providers, LangChain tools
│   ├── apis/          # FastAPI routes (match, agent, /api/stats), schemas, and demo data
│   ├── db/            # DuckDB archive: load, ingest, verify, and ready-made stats queries
│   ├── eval/          # Question sets with gold answers, and the scoring runner
│   ├── src/           # Reusable formatting and analytics engine
│   ├── tests/         # Offline engine, API, DuckDB, and stats-chat tests
│   ├── notebooks/     # API and feature exploration
│   └── requirements.txt
├── demo/              # Single-process Streamlit demo
└── frontend/
    └── ui/            # Responsive Vinext/React match-centre UI
```

## What is included

- Compact match scorecards sized for chat platforms
- Live/recent match, standings, player-card, and preview formatters
- Unicode Manhattan, win-probability, worm, partnership, and phase charts
- Momentum, turning-point, player-impact, and pitch-profile analytics
- Every IPL season from 2008: Cricsheet is the canonical archive, the scorecard is derived from the same deliveries as the ball-by-ball, ESPN adds commentary, and each match is consistency-checked
- Momentum-shift detection from deliveries and commentary: collapses, scoring bursts, squeezes, chase-equation swings, and missed chances
- Multi-source cricket search that routes natural language to match, player, standings, and analytics tools
- Pandit, a chat-first assistant on an observable LangGraph workflow; Groq (then OpenRouter, then local Gemma) summarises grounded match facts, and answers render match cards in the chat
- Stats questions across every IPL match, answered from a local DuckDB archive: the LLM (or keyword rules without one) picks verified queries, SQL computes every number, and any number in the reply that is not in the tables is rejected
- Robust question handling: first names and fan nicknames ("Rashid", "SKY"), batter and bowler in either order ("Bumrah vs Kohli"), a question back with one choice per player for shared surnames ("Pandya"), and a clear "outside the IPL archive" reply for WPL, Tests, other leagues, auctions and predictions
- Follow-up questions: "And in 2017?" or "What's his strike rate at the death?" are rewritten into a standalone question from the conversation before planning, so they work with or without an LLM
- A ground report: one ground against the league in the same seasons (par score by season, phase scoring, how wickets fall, chase success)
- A Stats workspace: leaders ranked by any stat and filtered by team, records, phases, players, matchups, teams (season by season, head to head, finals) and grounds
- An evaluation suite of 299 questions with gold answers, scoring accuracy, completeness, entity accuracy, paraphrase consistency, clarify and refusal behaviour
- A deterministic entity-resolution node that corrects team typos, resolves relative years, identifies teams/players/match terms/venues, carries conversational context, and preserves list intent
- A collapsible live trace pane showing graph traversal, conditional branches, intermediate summaries, state patches, and UI actions
- Chat-first UI: archive rail (every season, search), Pandit conversation with match and list cards, and a match workspace (overview, scorecard, ball by ball, momentum)
- Natural-language UI actions: filter the archive, open a historical match, switch match tabs, navigate sections, and change theme
- A grounded match story on every match overview, built only from scorecard, impact analytics, and commentary key moments (no model-generated numbers)
- Shareable match links with Back/Forward support, a copy-summary action, and keyboard shortcuts (`/` to search, `Esc` to close panels)
- In-app Docs drawer describing every public contract, source, tool owner, LangGraph state, transition, and fallback level
- Stable demo data so the UI remains useful without network access

## Run locally

### Backend

Python 3.10 or newer is required.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
python -m backend.apis
```

The API is available at `http://127.0.0.1:8000`, with interactive documentation at `http://127.0.0.1:8000/docs`.

Pandit's LLM keys live in `backend/.env` (git-ignored, loaded at startup; shell variables win). Copy the template and add your keys:

```bash
cp backend/.env.example backend/.env   # then set GROQ_API_KEY and OPENROUTER_API_KEY
```

Providers are tried in `LLM_PROVIDERS` order (default `groq,openrouter,gemma`); models are set with `GROQ_MODEL` (answers; default `openai/gpt-oss-120b,qwen/qwen3.8-27b`), `GROQ_PLAN_MODEL` (picks stats queries; default `openai/gpt-oss-20b,qwen/qwen3.8-27b,openai/gpt-oss-120b`) and `OPENROUTER_MODEL`. Groq's free tier limits each model separately (8k tokens a minute, 200k a day, roughly 80-100 questions), so the settings are lists: a rate-limited model hands over to the next one before Pandit falls back to OpenRouter. The LLM never invents data: it picks verified queries and summarises what they return. If no provider answers, or an answer contains a number that is not in the facts, Pandit replies with a grounded recap instead. `GET /api/agent/status` shows which provider is live.

### Asking Pandit about stats

With the stats archive built (below), Pandit answers questions across matches as well as about one match:

- "Who hit the most sixes in IPL 2024?", "Purple Cap 2013?", "most economical bowler for MI"
- "How has Kohli done against Bumrah?", "Compare Dhoni and Pant as batters"
- "How many titles have CSK won?", "How did RCB do in 2025?", "MI vs CSK head to head"
- "Best death bowlers in 2024", "Average first innings score at Chepauk", "Highest team total ever"
- "What's the pitch like at Chepauk?" (ground report), "Rashid's IPL economy", "Bumrah vs Kohli"
- "Pandya's batting record" asks which Pandya, with a button for each; "Most runs in WPL 2024" says it is outside the IPL archive
- Any metric on any slice of balls: "Kohli's average in finals", "Kohli chasing vs batting first", "strike rate in the last
  4 overs of successful chases since 2023", "best strike rate when the required rate is above 12", "economy after winning
  the toss", "most runs at Wankhede", "highest score in an IPL final", "death-overs run rate by season"
- Hat-tricks, player-of-the-match awards, the toss, impact-player substitutes, partnerships and opening pairs
- Judgment words get a stated definition: "best finishers" = strike rate in overs 16-20 of won chases, shrunk toward the
  league so a short hot streak cannot top the list; "most ahead of his era" = strike rate against the league that season

The planner (LLM tool calling, with keyword rules as fallback) chooses archive queries, the tables appear under the answer, and the trace pane shows the plan and the commands. A coverage guard checks that every filter the question states (finals, chasing, a ground, against a team, after winning the toss, ...) is applied by the plan; a call that cannot apply it is rewritten as a slice that can, or the reply names the filter it could not apply instead of answering a nearby question. Spin or pace, batting hand and home grounds are not in the archive yet, and Pandit says so. Single-match questions ("Where did the 2025 final turn?") still open the match.

Accuracy is measured in [`backend/eval`](backend/eval/README.md) on 299 questions. Scores on questions the code was tuned on are high (v1 development 100%, v2 robustness set 100% with keyword rules and 98% with the Groq planner). The fair numbers are the first runs on held-out questions: v1 held-out 79% (keyword rules), 84% (Groq planner) and 92% (full app, Groq planning and writing); v2 held-out 79% for both planners. In the full-app run, 35 of 38 LLM-written answers kept every number from the tables; the other 3 were replaced by the table summary.

### Ball-by-ball data (recommended)

Download Cricsheet's IPL archive once so every historical match has complete deliveries offline:

```bash
python -m backend.src.cricsheet sync
```

Files land in `backend/data/cricsheet/ipl` (git-ignored; override with `CRICSHEET_DIR`). Re-run after each season to pick up new matches. Cricsheet files are named by Cricinfo match ID, so archive URLs match Cricinfo's.

For a synced match, everything comes from one set of deliveries:

1. **Cricsheet** deliveries → ball-by-ball, a derived scorecard (batting, bowling, extras, fall of wickets, partnerships, overs), analytics, and momentum shifts.
2. **ESPN play-by-play** (`cricdata.match_ball_by_ball`) adds commentary text to those deliveries.
3. **ESPNcricinfo scorecard** cross-checks the innings totals (`verified` / `mismatch`), and the recorded result is checked against the derived scorecard.

Cricinfo responses are cached on disk per match (`backend/data/cache`, override with `CRICBOT_CACHE_DIR`) and requests are throttled, because `cricdata` scrapes without a contract. Matches not yet in Cricsheet fall back to ESPN play-by-play, then Cricinfo commentary pages, then over summaries; coverage is always labelled.

### Stats archive (for the Stats view)

The Stats view (the **∑** button, or **Stats** on mobile) and `/api/stats/*` read a local DuckDB file built from the same Cricsheet data:

```bash
python -m backend.db.cricsheet_load --sync     # ~20 s: downloads Cricsheet and loads every IPL delivery
```

An archive built before October 2026 lacks the toss, player-of-the-match, fielding and impact-substitute tables: run
`python -m backend.db.cricsheet_load` once (no download needed; it rebuilds only the Cricsheet tables, ESPN data is kept).
Until then those questions say the data is not loaded.

Batting hand and bowling type (spin or pace) come from ESPN player profiles, which Cricsheet lacks:

```bash
python -m backend.db.player_styles --fetch 200   # ~8 min once: about 190 scorecards cover every IPL player
```

Without it, questions such as "Kohli against spin" or "best left-handed batter" say the styles are not loaded.

It covers season and all-time leaders (any sort, any team), records, powerplay / middle / death splits, player profiles, batter v bowler, venues, a ground report, champions, team seasons and head to head. The **Explore** tab builds any slice (who, measure, filters, compare by) with presets such as best finishers, Kohli chasing vs batting first and death-overs run rate by season; **Stands** lists partnerships and pairs; Records adds player-of-the-match awards, hat-tricks and impact substitutes; Grounds adds the toss. The same queries run from the command line:

```bash
python -m backend.db.queries top-batters --year 2024 --sort sixes
python -m backend.db.queries matchup "Virat Kohli" "Jasprit Bumrah"
python -m backend.db.queries ground chidambaram --year 2018-2025
``` Optionally, `python -m backend.db.ingest` adds ESPN commentary and the official scorecard for cross-checking. Details, schema and accuracy checks are in [`backend/db/README.md`](backend/db/README.md).

### Frontend

```bash
cd frontend/ui
cp .env.example .env.local
npm install
npm run dev
```

Open `http://localhost:3000`. The backend automatically labels and uses its local demo fallback if the cricket provider is unavailable.

Leave `NEXT_PUBLIC_API_URL` blank for the recommended local setup. The frontend then calls same-origin `/api` routes, and the Vite development server proxies those requests to FastAPI at `http://127.0.0.1:8000`. Set the variable only when the API is hosted on a different origin.

### Optional local Gemma service

Pandit uses deterministic tools even when Gemma is offline. To enable conversational synthesis on the configured Mac serving project, run this in a separate terminal before starting Cricbot:

```bash
cd /Users/saketm10/Projects/foundation-ai-platform/mac-serving
.venv/bin/mac-serve serve
```

Confirm readiness at `http://127.0.0.1:8080/readyz`. Never expose port `8080` directly to the internet.

## Demo dashboard (Streamlit)

A single-process demo that imports the backend directly (no FastAPI or Node):

```bash
pip install -r demo/requirements.txt
streamlit run demo/streamlit_app.py        # http://localhost:8501
```

It has season and match pickers, Pandit chat, overview with provenance, scorecard, a filterable ball-by-ball table, and a Momentum tab with a worm chart (detected shifts highlighted on the innings where they happened) and runs-per-over. It reads the same `backend/.env` keys and Cricsheet data as the main app; without an LLM, Pandit answers with grounded recaps.

## Share a temporary preview with ngrok

Start the backend and frontend as described above, then run:

```bash
ngrok http 3000
```

Share the HTTPS forwarding URL printed by ngrok. The single frontend tunnel also carries `/api` traffic through the local proxy, so search, match details, documentation, and Pandit work without a second public tunnel. Your friend may need to accept ngrok's one-time browser warning. The link remains available only while the Mac, Cricbot services, and ngrok process are running.

## API surface

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/api/health` | Service health and version |
| `GET` | `/api/dashboard` | Complete dashboard payload used by the UI |
| `GET` | `/api/ipl/seasons` | Supported IPL archive seasons |
| `GET` | `/api/ipl/matches?season=2025&q=RCB&limit=24&offset=0` | Search and page canonical IPL match cards |
| `GET` | `/api/ipl/matches/{series_slug}/{match_slug}` | Match info, innings, balls, coverage, analytics, and provenance |
| `GET` | `/api/search?q=...` | Route natural-language search across grounded cricket tools |
| `GET` | `/api/agent/status` | Check the local Gemma model and completion contract |
| `GET` | `/api/agent/graph` | Inspect Pandit's nodes, edges, and conditional transitions |
| `GET` | `/api/docs/catalog` | Machine-readable API, source, tool, state, and transition documentation |
| `POST` | `/api/agent/chat` | Ask Pandit a grounded question and receive UI actions |
| `POST` | `/api/agent/chat/stream` | Stream node traces as NDJSON, followed by the final answer |
| `POST` | `/api/scorecard/render` | Render a compact scorecard from match payloads |
| `POST` | `/api/analytics` | Impact, momentum, turning points, and pitch profile |
| `POST` | `/api/views/live` | Format an existing live-match payload |
| `POST` | `/api/views/standings` | Format a standings payload |
| `POST` | `/api/views/player` | Format a player profile and career summary |
| `POST` | `/api/views/preview` | Format a pre-match preview |
| `GET` | `/api/stats/status` | Whether the DuckDB archive is built, and what it holds |
| `GET` | `/api/stats/seasons` | Coverage and scoring per IPL season |
| `GET` | `/api/stats/batting`, `/api/stats/bowling` | Leaderboards (`year`, `sort`, `team`, `min_balls`, `limit`) |
| `GET` | `/api/stats/records`, `/api/stats/phase` | Records; powerplay / middle / death leaders |
| `GET` | `/api/stats/players?q=...`, `/api/stats/players/{player_id}` | Player search; one player by year and by phase |
| `GET` | `/api/stats/matchup?batter=...&bowler=...` | Every ball a batter faced from a bowler |
| `GET` | `/api/stats/h2h`, `/api/stats/teams/{code}`, `/api/stats/champions` | Head to head, team seasons, finals and titles |
| `GET` | `/api/stats/venues`, `/api/stats/grounds/{name}` | Grounds; one ground against the league |
| `GET` | `/api/stats/slice` | Any metric on any slice of balls: filters (incl. `bowl_kind`, `bat_hand`, `venue_type`), splits, rank by career / season / match (`per`), season-adjusted and sample-adjusted rankings |
| `GET` | `/api/stats/pitch/{match_id}`, `/home`, `/par` | One match's pitch profile; home and away records; runs and wickets per over against the season's par |
| `GET` | `/api/stats/partnerships`, `/awards`, `/hattricks`, `/impact`, `/toss` | Stands and pairs, player of the match, hat-tricks, impact substitutes, the toss |
| `GET` | `/api/live` | Fetch live matches through `cricdata` |
| `GET` | `/api/matches/{series_slug}/{match_slug}` | Fetch the complete live match bundle |

## Python usage

```python
from cricdata import CricinfoClient

from backend.src.cricbot_views import match_slugs
from backend.src.scorecard_renderer import build_compact_scorecard

ci = CricinfoClient()
fixtures = ci.series_fixtures("ipl-2025-1449924")
series_slug, match_slug = match_slugs(fixtures["content"]["matches"][0])

info = ci.match_info(series_slug, match_slug)
scorecard = ci.match_scorecard(series_slug, match_slug)

print(build_compact_scorecard(
    info,
    scorecard,
    phases=True,
    charts=True,
    partnerships=True,
    fow=False,
))
```

## Backend modules

| Module | Responsibility |
|---|---|
| `backend/src/scorecard_helpers.py` | Payload normalization and innings extraction |
| `backend/src/scorecard_charts.py` | Dependency-free text charts |
| `backend/src/scorecard_renderer.py` | Main compact-scorecard renderer |
| `backend/src/cricbot_views.py` | Match, standings, player, preview, and fetch helpers |
| `backend/src/ipl_data.py` | IPL archive access, provider normalization, and ball-data fallback ladder |
| `backend/src/cricsheet.py` | Cricsheet sync, match index, and delivery normalization |
| `backend/src/cricsheet_scorecard.py` | Scorecard derivation from deliveries (Laws-of-cricket conventions) |
| `backend/src/momentum_shifts.py` | Momentum-shift detection from deliveries, commentary, and the chase equation |
| `backend/src/ipl_teams.py` | Franchise codes and colours across renamed and defunct teams |
| `backend/src/impact_lab.py` | Experimental momentum, impact, and pitch analytics |
| `backend/agent/tools.py` | LangChain search tools and deterministic query routing |
| `backend/agent/entities.py` | Team/player/year/venue extraction, typo correction, temporal resolution, and conversational carry-over |
| `backend/agent/service.py` | LangGraph state, nodes, trace events, grounded responses, and UI actions |
| `backend/agent/llm.py` | Groq/OpenRouter/Gemma provider chain, tool-call planning and `.env` loading |
| `backend/agent/stats.py` | Stats questions: tool specs, planner (LLM + keyword rules), player index, query runner, facts and chat card |
| `backend/agent/slices.py` | Planner side of slices: wording parser, slice/family rules, definitions for "best X", the coverage guard |
| `backend/db/player_styles.py` | Batting hand and bowling type from ESPN scorecards (cached; a greedy cover picks the fewest matches) |
| `backend/db/store.py` | DuckDB schema and fast typed bulk insert |
| `backend/db/cricsheet_load.py` | Load Cricsheet deliveries, the people register, and match facts (toss, player of the match, fielders, impact subs) |
| `backend/db/analytics.py` | Slice compiler (any metric, filter, split; season-adjusted and shrunk rankings), match-state view, hat-tricks, toss, awards, impact subs, partnerships |
| `backend/db/ingest.py`, `parse.py` | Ingest ESPN matches (scorecards, deliveries, commentary) through `cricdata` |
| `backend/db/evaluate.py` | Check ball-by-ball data against the official scorecard |
| `backend/db/queries.py` | Verified stats queries shared by the CLI, `/api/stats`, Pandit and the eval |
| `backend/eval/build_qna*.py`, `run_qna.py` | Build the question sets from the archive; score Pandit on them |
| `backend/agent/match_facts.py` | Facts sheet (for the LLM) and chat card (for the UI) for a resolved match |
| `backend/agent/gemma.py` | Local Gemma `/v1/completions` client and readiness checks |
| `backend/apis/main.py` | FastAPI application and provider routes |
| `backend/apis/docs_catalog.py` | In-app living documentation contract |

## Testing

```bash
python backend/tests/test_scorecard.py
python backend/tests/test_api.py
python backend/tests/test_agent.py
python backend/tests/test_ball_sources.py
python backend/tests/test_cricsheet_scorecard.py
python backend/tests/test_momentum_shifts.py
python backend/tests/test_llm.py
python -m pytest backend/tests          # everything, including the DuckDB and stats-chat tests

cd frontend/ui
npm test
```

Pandit's accuracy (needs the DuckDB archive; the rules planner needs no LLM keys and takes about a minute):

```bash
python -m backend.eval.run_qna --planner rules                                          # v1 development set
python -m backend.eval.run_qna --planner rules --dataset backend/eval/qna_ipl_v2.jsonl  # v2 robustness set
python -m backend.eval.run_qna --planner hybrid --dataset backend/eval/qna_ipl_v2.jsonl # with the LLM planner
python -m backend.eval.run_qna --planner rules --dataset backend/eval/qna_ipl_v4_round.jsonl  # styles, units, home, counts
```

All backend tests use synthetic Cricsheet- and ESPNcricinfo-shaped payloads and mocked LLM providers. The suite needs no network access and never calls Groq or OpenRouter.

## Data notes

- ESPNcricinfo win probability is reported for the batting side. The helper layer inverts it when tracking one team across both innings.
- IPL list requests are cached in-process. Match detail uses independent best-effort provider calls so one failed sub-resource does not erase a usable scorecard.
- Delivery coverage is explicit: dedicated ball feed, historical commentary, over summaries, demo fallback, or unavailable. Historical commentary can be partial.
- The per-match impact and pitch models are exploratory. A single match is not enough to rate a venue; the ground report aggregates across seasons and compares with the league in the same seasons, and it describes outcomes (surface, boundary size, dew and teams together), not the pitch itself.
- Not yet in the archive: fielding (catches, stumpings, run outs are in the raw Cricsheet files but not loaded), bowling style, awards, and ball speed.
- Data comes from ESPNcricinfo through the unaffiliated `cricdata` package. Review the provider's terms before public deployment.
