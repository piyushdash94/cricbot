# Cricbot

A local-first IPL archive and cricket assistant that turns ESPNcricinfo-shaped data into searchable match pages, scorecards, delivery timelines, analytics, and natural-language UI actions.

The repository now has two clear surfaces:

```text
cricbot/
├── backend/
│   ├── agent/         # Pandit orchestration, Gemma client, and LangChain tools
│   ├── apis/          # FastAPI routes, schemas, and demo data
│   ├── src/           # Reusable formatting and analytics engine
│   ├── tests/         # Offline engine and API contract tests
│   ├── notebooks/     # API and feature exploration
│   └── requirements.txt
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

Providers are tried in `LLM_PROVIDERS` order (default `groq,openrouter,gemma`); models are set with `GROQ_MODEL` and `OPENROUTER_MODEL`. The LLM only summarises facts Pandit retrieved. If no provider answers, or an answer contains a number that is not in the facts, Pandit replies with a grounded recap instead. `GET /api/agent/status` shows which provider is live.

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
| `backend/agent/llm.py` | Groq/OpenRouter/Gemma provider chain and `.env` loading |
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

cd frontend/ui
npm test
```

All backend tests use synthetic Cricsheet- and ESPNcricinfo-shaped payloads and mocked LLM providers. The suite needs no network access and never calls Groq or OpenRouter.

## Data notes

- ESPNcricinfo win probability is reported for the batting side. The helper layer inverts it when tracking one team across both innings.
- IPL list requests are cached in-process. Match detail uses independent best-effort provider calls so one failed sub-resource does not erase a usable scorecard.
- Delivery coverage is explicit: dedicated ball feed, historical commentary, over summaries, demo fallback, or unavailable. Historical commentary can be partial.
- The impact and pitch models are exploratory. A single match is not enough to rate a venue; aggregate by format, season, and ground before making strong claims.
- Data comes from ESPNcricinfo through the unaffiliated `cricdata` package. Review the provider's terms before public deployment.
