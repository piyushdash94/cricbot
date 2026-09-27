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
- FastAPI endpoints for the IPL 2023–2025 archive, canonical match detail, scorecards, analytics, and views
- Multi-source cricket search that routes natural language to match, player, standings, and analytics tools
- Pandit, a right-side assistant powered by an observable LangGraph workflow and the local Gemma 4 completion server
- A collapsible live trace pane showing graph traversal, conditional branches, intermediate summaries, state patches, and UI actions
- Archive-first UI with season filters, historical match search, scorecards, available ball-by-ball commentary, provenance, and analytics
- Natural-language UI actions: open a historical match, switch match tabs, navigate sections, and change theme
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

Pandit expects the local Gemma server at `http://127.0.0.1:8080` using the OpenAI-compatible `/v1/completions` contract. Its defaults are in `backend/.env.example`; override `GEMMA_API_BASE`, `GEMMA_MODEL`, or `MAC_SERVING_API_KEY` in your shell when needed. The dashboard and deterministic tools continue to work when Gemma is offline.

### Frontend

```bash
cd frontend/ui
cp .env.example .env.local
npm install
npm run dev
```

Open `http://localhost:3000`. If `NEXT_PUBLIC_API_URL` is omitted, the frontend calls `http://127.0.0.1:8000`. The backend automatically labels and uses its local demo fallback if the cricket provider is unavailable.

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
| `backend/src/impact_lab.py` | Experimental momentum, impact, and pitch analytics |
| `backend/agent/tools.py` | LangChain search tools and deterministic query routing |
| `backend/agent/service.py` | LangGraph state, nodes, trace events, grounded responses, and UI actions |
| `backend/agent/gemma.py` | Local Gemma `/v1/completions` client and readiness checks |
| `backend/apis/main.py` | FastAPI application and provider routes |
| `backend/apis/docs_catalog.py` | In-app living documentation contract |

## Testing

```bash
python backend/tests/test_scorecard.py
python backend/tests/test_api.py
python backend/tests/test_agent.py

cd frontend/ui
npm test
```

All backend tests use synthetic ESPNcricinfo-shaped payloads. The core suite does not require network access or API keys.

## Data notes

- ESPNcricinfo win probability is reported for the batting side. The helper layer inverts it when tracking one team across both innings.
- IPL list requests are cached in-process. Match detail uses independent best-effort provider calls so one failed sub-resource does not erase a usable scorecard.
- Delivery coverage is explicit: dedicated ball feed, historical commentary, over summaries, demo fallback, or unavailable. Historical commentary can be partial.
- The impact and pitch models are exploratory. A single match is not enough to rate a venue; aggregate by format, season, and ground before making strong claims.
- Data comes from ESPNcricinfo through the unaffiliated `cricdata` package. Review the provider's terms before public deployment.
