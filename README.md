# Cricbot

A full-stack cricket match centre that turns ESPNcricinfo-shaped data into compact scorecards, text charts, live views, and match analytics.

The repository now has two clear surfaces:

```text
cricbot/
├── backend/
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
- FastAPI endpoints for the dashboard, scorecard renderer, analytics, and views
- Responsive interactive UI with match switching, detail tabs, search, theme switching, and summary sharing
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

### Frontend

```bash
cd frontend/ui
cp .env.example .env.local
npm install
npm run dev
```

Open `http://localhost:3000`. If `NEXT_PUBLIC_API_URL` is omitted, the frontend uses its built-in demo dataset.

## API surface

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/api/health` | Service health and version |
| `GET` | `/api/dashboard` | Complete dashboard payload used by the UI |
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
| `backend/src/impact_lab.py` | Experimental momentum, impact, and pitch analytics |
| `backend/apis/main.py` | FastAPI application and provider routes |

## Testing

```bash
python backend/tests/test_scorecard.py
python backend/tests/test_api.py

cd frontend/ui
npm test
```

All backend tests use synthetic ESPNcricinfo-shaped payloads. The core suite does not require network access or API keys.

## Data notes

- ESPNcricinfo win probability is reported for the batting side. The helper layer inverts it when tracking one team across both innings.
- `cricdata` caches match sub-resources by match slug, so scorecard, overs, partnerships, and fall-of-wickets views generally share one upstream request.
- The impact and pitch models are exploratory. A single match is not enough to rate a venue; aggregate by format, season, and ground before making strong claims.
- Data comes from ESPNcricinfo through the unaffiliated `cricdata` package. Review the provider's terms before public deployment.
