"""Cricbot demo dashboard.

    pip install -r demo/requirements.txt
    streamlit run demo/streamlit_app.py

Uses the backend modules directly (no FastAPI or Node needed): the same
archive, match detail, momentum shifts, and Pandit agent as the main app.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.agent.llm import load_env_file  # noqa: E402

load_env_file()  # backend/.env (git-ignored) holds GROQ_API_KEY / OPENROUTER_API_KEY

from backend.agent.service import CricbotAgent  # noqa: E402
from backend.src import cricsheet  # noqa: E402
from backend.src.ipl_data import all_seasons, get_match_detail, list_ipl_matches  # noqa: E402

# Categorical slots 1-2 of the validated reference palette, dark steps, checked
# against the #1a1a19 chart surface (lightness, chroma, CVD, contrast all pass).
SERIES = ["#3987e5", "#d95926"]
SURFACE = "#1a1a19"
INK, INK_MUTED, GRID = "#ffffff", "#c3c2b7", "rgba(255,255,255,0.08)"
SHIFT_LABELS = {
    "wicket_cluster": "Collapse", "scoring_burst": "Scoring burst", "squeeze": "Squeeze",
    "chase_swing": "Chase swing", "missed_chance": "Missed chance",
}

st.set_page_config(page_title="Cricbot · IPL match intelligence", page_icon="🏏", layout="wide")
st.markdown(
    """<style>
    .block-container { padding-top: 1.6rem; }
    .result { font-size: 1.05rem; font-weight: 700; }
    .shift { padding: .6rem .8rem; margin-bottom: .5rem; border-left: 3px solid #c3c2b7; background: rgba(255,255,255,.03); border-radius: 0 8px 8px 0; }
    .shift.decisive { border-left-color: #fab219; background: rgba(250,178,25,.07); }
    .shift small { color: #c3c2b7; }
    </style>""",
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------- data access

@st.cache_data(ttl=600, show_spinner=False)
def seasons() -> list[int]:
    return all_seasons()


@st.cache_data(ttl=600, show_spinner=False)
def matches(season: int, query: str) -> dict:
    return list_ipl_matches(season=season, query=query, limit=100)


@st.cache_data(ttl=3600, show_spinner="Loading scorecard, deliveries, commentary, and momentum…")
def detail(series_slug: str, match_slug: str) -> dict:
    return get_match_detail(series_slug, match_slug)


@st.cache_resource
def agent() -> CricbotAgent:
    return CricbotAgent()


@st.cache_data(ttl=120, show_spinner=False)
def llm_status() -> dict:
    return agent().status()


def team_colors(match: dict) -> dict[str, str]:
    """Fixed slot per team in match order, keyed by both code and full name."""
    colors = {}
    for index, team in enumerate(match["teams"][:2]):
        colors[team["abbr"]] = colors[team["name"]] = SERIES[index]
    return colors


def chart_layout(fig: go.Figure, height: int, *, legend: bool = True) -> go.Figure:
    fig.update_layout(
        height=height, margin=dict(l=8, r=8, t=36, b=8),
        paper_bgcolor=SURFACE, plot_bgcolor=SURFACE, font=dict(color=INK_MUTED, size=13),
        hoverlabel=dict(bgcolor="#262624", font_color=INK, bordercolor="rgba(255,255,255,0.2)"),
        showlegend=legend, legend=dict(orientation="h", y=-0.22, x=0, font=dict(color=INK)),
    )
    fig.update_xaxes(gridcolor=GRID, zeroline=False, linecolor=GRID, tickfont=dict(color=INK_MUTED))
    fig.update_yaxes(gridcolor=GRID, zeroline=False, linecolor=GRID, tickfont=dict(color=INK_MUTED))
    return fig


def legal(ball: dict) -> bool:
    return "wd" not in str(ball["event"]) and "nb" not in str(ball["event"])


def worm_frame(data: dict) -> pd.DataFrame:
    rows = []
    for number in sorted({ball["inning"] for ball in data["balls"]}):
        runs = wickets = balls = 0
        rows.append({"inning": number, "over": 0.0, "runs": 0, "wickets": 0, "label": "0.0", "wicket": False, "event": "", "title": ""})
        for ball in (item for item in data["balls"] if item["inning"] == number):
            runs += int(ball["runs"] or 0)
            wickets += 1 if ball["wicket"] else 0
            balls += 1 if legal(ball) else 0
            rows.append({"inning": number, "over": round(balls / 6, 3), "runs": runs, "wickets": wickets, "label": ball["label"],
                         "wicket": ball["wicket"], "event": ball["event"], "title": ball.get("title", "")})
    return pd.DataFrame(rows)


def over_position(label: str) -> float:
    over, _, ball = str(label).partition(".")
    try:
        return int(over) + min(int(ball or 0), 6) / 6
    except ValueError:
        return 0.0


def worm_chart(data: dict) -> go.Figure:
    frame = worm_frame(data)
    colors = team_colors(data["match"])
    fig = go.Figure()
    # Each momentum shift is highlighted on its own innings' line: a wide,
    # translucent segment in the favoured team's colour, explained on hover.
    for shift in data["analytics"].get("shifts", []):
        start, end = over_position(shift["from"]) - 1 / 6, over_position(shift["to"])
        part = frame[(frame["inning"] == shift["inning"]) & (frame["over"] >= start - 1e-9) & (frame["over"] <= end + 1e-9)]
        if part.empty:
            continue
        label = f"{SHIFT_LABELS[shift['type']]}{' (largest)' if shift.get('decisive') else ''}"
        fig.add_trace(go.Scatter(
            x=part["over"], y=part["runs"], mode="lines", showlegend=False, name=label,
            line=dict(color=colors.get(shift["favours"], INK_MUTED), width=14 if shift.get("decisive") else 10),
            opacity=0.35, hoverinfo="text",
            hovertext=f"<b>{label}</b> · {shift['innings_team']} overs {shift['from']}–{shift['to']}<br>{shift['headline']}<br>favours {shift['favours']}",
        ))
        if shift.get("decisive"):
            fig.add_annotation(x=part["over"].iloc[-1], y=part["runs"].iloc[-1], text=f"{label}: {shift['headline']}",
                               showarrow=True, arrowcolor=INK_MUTED, ax=40, ay=-46, font=dict(color=INK, size=12),
                               bgcolor="#262624", bordercolor="rgba(255,255,255,0.2)", borderpad=4)
    for inning in data["innings"]:
        part = frame[frame["inning"] == inning["number"]]
        color = colors.get(inning["team"], INK_MUTED)
        fig.add_trace(go.Scatter(
            x=part["over"], y=part["runs"], mode="lines", name=f"{inning['team']} {inning['runs']}/{inning['wickets']}",
            line=dict(color=color, width=2), customdata=part[["label", "wickets"]],
            hovertemplate=f"<b>{inning['team']}</b> %{{customdata[0]}}<br>%{{y}}/%{{customdata[1]}}<extra></extra>",
        ))
        falls = part[part["wicket"]]
        fig.add_trace(go.Scatter(
            x=falls["over"], y=falls["runs"], mode="markers", showlegend=False, name=f"{inning['team']} wickets",
            marker=dict(size=9, color=color, line=dict(color=SURFACE, width=2)), customdata=falls[["label", "title"]],
            hovertemplate=f"<b>Wicket</b> · {inning['team']} %{{customdata[0]}}<br>%{{customdata[1]}}<br>%{{y}} runs<extra></extra>",
        ))
        # Direct label at the line end.
        if not part.empty:
            fig.add_annotation(x=part["over"].iloc[-1], y=part["runs"].iloc[-1], text=inning["team"], showarrow=False,
                               xanchor="left", xshift=6, font=dict(color=INK, size=12))
    fig.update_xaxes(title="Overs", range=[0, 21])
    fig.update_yaxes(title="Runs", rangemode="tozero")
    fig.update_layout(hovermode="closest", legend=dict(orientation="h", y=-0.22, x=0, font=dict(color=INK)))
    return chart_layout(fig, 440)


def manhattan_chart(data: dict) -> go.Figure:
    colors = team_colors(data["match"])
    innings = data["innings"]
    per_over = {inning["number"]: {} for inning in innings}
    for ball in data["balls"]:
        stats = per_over.setdefault(ball["inning"], {}).setdefault(int(ball.get("over") or 0), {"runs": 0, "wickets": 0})
        stats["runs"] += int(ball["runs"] or 0)
        stats["wickets"] += 1 if ball["wicket"] else 0
    peak = max((stats["runs"] for overs in per_over.values() for stats in overs.values()), default=10)
    fig = make_subplots(rows=1, cols=len(innings), shared_yaxes=True, horizontal_spacing=0.04,
                        subplot_titles=[f"{inning['team']} innings" for inning in innings])
    for column, inning in enumerate(innings, 1):
        overs = per_over.get(inning["number"], {})
        xs = sorted(overs)
        fig.add_trace(go.Bar(
            x=[over + 1 for over in xs], y=[overs[over]["runs"] for over in xs], name=inning["team"],
            marker=dict(color=colors.get(inning["team"], INK_MUTED), cornerradius=4, line=dict(width=0)),
            customdata=[overs[over]["wickets"] for over in xs],
            hovertemplate=f"<b>{inning['team']}</b> over %{{x}}<br>%{{y}} runs · %{{customdata}} wkt<extra></extra>",
        ), row=1, col=column)
        wicket_overs = [over for over in xs if overs[over]["wickets"]]
        fig.add_trace(go.Scatter(
            x=[over + 1 for over in wicket_overs], y=[overs[over]["runs"] + peak * 0.06 for over in wicket_overs],
            mode="text", text=["●" * overs[over]["wickets"] for over in wicket_overs], textfont=dict(color=INK, size=9),
            showlegend=False, hoverinfo="skip",
        ), row=1, col=column)
    fig.update_layout(bargap=0.15)
    fig.update_annotations(font=dict(color=INK, size=13))
    fig.update_xaxes(title="Over", dtick=5)
    fig.update_yaxes(title="Runs", row=1, col=1)
    return chart_layout(fig, 320, legend=False)


# ---------------------------------------------------------------- sidebar

with st.sidebar:
    st.title("🏏 Cricbot")
    st.caption("IPL match intelligence · demo dashboard")
    season = st.selectbox("Season", seasons(), index=0)
    query = st.text_input("Search", placeholder="RCB, Wankhede, final…")
    listing = matches(season, query.strip())
    options = listing["matches"]
    if not options:
        st.warning("No matches for this search.")
    labels = [f"{m['date'][:10]} · {m['title']} · {m['status_text']}" for m in options]
    picked = st.selectbox(f"Match ({len(options)})", range(len(options)), format_func=lambda i: labels[i]) if options else None

    st.divider()
    entries = cricsheet.entries()
    if entries:
        st.success(f"Cricsheet: {len(entries)} matches · {min(cricsheet.seasons())}–{max(cricsheet.seasons())}")
    else:
        st.info("Cricsheet not synced; using ESPNcricinfo or demo data.\n\n`python -m backend.src.cricsheet sync`")
    status = llm_status()
    if status.get("provider"):
        st.success(f"LLM: {status['provider']} · {status['model']}")
    else:
        st.warning("LLM offline: Pandit answers with grounded recaps.")
    st.caption(f"Archive source: {listing['source']}")

if picked is None:
    st.stop()

match_row = options[picked]
data = detail(match_row["series_slug"], match_row["match_slug"])
match = data["match"]

# ---------------------------------------------------------------- header

st.caption(f"{match['series_name']} · {match.get('label', '')} · {match['date'][:10]} · {match['venue']}")
cols = st.columns([1, 1, 2])
for column, team in zip(cols, match["teams"]):
    column.metric(f"{team['name']} · {team.get('score_info', '')}", team["score"])
with cols[2]:
    st.markdown(f"<div class='result'>{match['status_text']}</div>", unsafe_allow_html=True)
    consistency = data.get("consistency", {})
    badge = {"verified": "✅ verified", "single-source": "single source (not cross-checked)", "mismatch": "⚠️ mismatch", "demo": "⚠️ demo data"}.get(consistency.get("status"), "")
    st.caption(f"{data['ball_coverage']['label']} · {len(data['balls'])} deliveries · {data.get('commentary_count', 0)} with commentary · {badge}")
    if consistency.get("note"):
        st.caption(consistency["note"])

chat_tab, overview_tab, score_tab, balls_tab, momentum_tab = st.tabs(["💬 Pandit", "Overview", "Scorecard", "Ball by ball", "Momentum"])

# ---------------------------------------------------------------- Pandit

with chat_tab:
    key = f"chat::{match['match_slug']}"
    history = st.session_state.setdefault(key, [
        {"role": "assistant", "content": f"Ask me anything about {match['title']}: where it turned, a player's spell, or a specific over."},
    ])
    suggestion = None
    chips = st.columns(4)
    for column, prompt in zip(chips, ["Where did this match turn?", "Summarise the chase", "Who bowled the best spell?", "What happened in the last over?"]):
        if column.button(prompt, use_container_width=True):
            suggestion = prompt
    for message in history:
        with st.chat_message(message["role"], avatar="🏏" if message["role"] == "assistant" else None):
            st.markdown(message["content"])
            if message.get("via"):
                st.caption(message["via"])
    prompt = st.chat_input("Ask Pandit about this match…") or suggestion
    if prompt:
        history.append({"role": "user", "content": prompt})
        with st.spinner("Pulling the match facts…"):
            result = agent().answer(
                prompt,
                history=[{"role": item["role"], "content": item["content"]} for item in history[-8:-1]],
                ui_context={"series_slug": match["series_slug"], "match_slug": match["match_slug"],
                            "teams": [team["abbr"] for team in match["teams"]], "season": match["season"]},
            )
        reply = result["reply"]
        other = next((card for card in result.get("cards", []) if card["type"] == "match" and card["match"]["match_slug"] != match["match_slug"]), None)
        if other:
            reply += f"\n\n_That is about **{other['match']['title']}** ({other['match']['date'][:10]}); pick it in the sidebar to explore it._"
        via = f"{result['provider']} · {result['model']}" if result.get("model_used") else "grounded recap (no LLM)"
        history.append({"role": "assistant", "content": reply, "via": via})
        st.rerun()  # redraw so the conversation sits above the input

# ---------------------------------------------------------------- overview

with overview_tab:
    left, right = st.columns([3, 2])
    with left:
        st.subheader("How it was won")
        for inning in data["innings"]:
            bat = max(inning["batters"], key=lambda row: (row["runs"], -row["balls"]), default=None)
            bowl = max(inning["bowlers"], key=lambda row: (row["wickets"], -float(row["runs"] or 0)), default=None)
            line = f"**{inning['team']} {inning['runs']}/{inning['wickets']}** ({inning['overs']} ov)"
            if bat:
                line += f" — top score {bat['name']} {bat['runs']}{'*' if bat['not_out'] else ''} ({bat['balls']})"
            if bowl and bowl["wickets"]:
                line += f"; best bowling {bowl['name']} {bowl['wickets']}/{bowl['runs']}"
            st.markdown(line)
        decisive = next((shift for shift in data["analytics"].get("shifts", []) if shift.get("decisive")), None)
        if decisive:
            st.markdown(f"**Turning point:** {SHIFT_LABELS[decisive['type']].lower()} in the {decisive['innings_team']} innings, "
                        f"overs {decisive['from']}–{decisive['to']}: {decisive['headline']} ({decisive['score_before']} → {decisive['score_after']}), "
                        f"swinging it to {decisive['favours']}.")
        st.markdown(f"**Toss:** {data.get('toss')}  \n**Player of the match:** {', '.join(data.get('player_awards') or []) or 'Unavailable'}")
    with right:
        st.subheader("Provenance")
        st.dataframe(pd.DataFrame(data["sources"]).rename(columns={"name": "Feed", "provider": "Source", "available": "Available"}),
                     hide_index=True, use_container_width=True)
        checks = consistency.get("checks") or []
        if checks:
            st.dataframe(pd.DataFrame(checks), hide_index=True, use_container_width=True)
        internal = consistency.get("internal")
        if internal and internal.get("status") != "skipped":
            st.caption(f"Result vs scorecard: {internal['status']} ({internal.get('detail', '')})")

# ---------------------------------------------------------------- scorecard

with score_tab:
    for inning in data["innings"]:
        st.subheader(f"{inning['team']} · {inning['runs']}/{inning['wickets']} ({inning['overs']} ov, extras {inning['extras']})")
        batting = pd.DataFrame([{
            "Batter": f"{row['name']}{'*' if row['not_out'] else ''}",
            "Dismissal": (row["dismissal"].get("long") if isinstance(row["dismissal"], dict) else row["dismissal"]) or ("not out" if row["not_out"] else ""),
            "R": row["runs"], "B": row["balls"], "4s": row["fours"], "6s": row["sixes"], "SR": row["strike_rate"],
        } for row in inning["batters"]])
        bowling = pd.DataFrame([{
            "Bowler": row["name"], "O": row["overs"], "M": row["maidens"], "R": row["runs"], "W": row["wickets"], "Econ": row["economy"], "Dots": row["dots"],
        } for row in inning["bowlers"]])
        left, right = st.columns([3, 2])
        left.dataframe(batting, hide_index=True, use_container_width=True)
        right.dataframe(bowling, hide_index=True, use_container_width=True)

# ---------------------------------------------------------------- ball by ball

with balls_tab:
    balls = pd.DataFrame(data["balls"])
    if balls.empty:
        st.info("No delivery-level data for this match.")
    else:
        teams_by_innings = {inning["number"]: inning["team"] for inning in data["innings"]}
        left, middle, right = st.columns([2, 2, 3])
        innings_pick = left.selectbox("Innings", ["All", *[f"{n}: {t}" for n, t in teams_by_innings.items()]])
        kind = middle.selectbox("Show", ["Every ball", "Wickets", "Boundaries", "With commentary"])
        text = right.text_input("Filter text", placeholder="bowler, batter, word in commentary…")
        view = balls.copy()
        if innings_pick != "All":
            view = view[view["inning"] == int(innings_pick.split(":")[0])]
        if kind == "Wickets":
            view = view[view["wicket"]]
        elif kind == "Boundaries":
            view = view[view["boundary"]]
        elif kind == "With commentary" and "commentary" in view:
            view = view[view["commentary"].notna()]
        if text:
            haystack = view[[column for column in ("title", "text", "commentary") if column in view]].astype(str).agg(" ".join, axis=1)
            view = view[haystack.str.contains(text, case=False)]
        view = view.assign(team=view["inning"].map(teams_by_innings))
        columns = [column for column in ("team", "label", "event", "title", "text", "commentary", "score") if column in view]
        st.caption(f"{len(view)} of {len(balls)} deliveries · {data['ball_coverage']['note']}")
        st.dataframe(view[columns].rename(columns={"team": "Team", "label": "Ball", "event": "Event", "title": "Delivery", "text": "Description", "commentary": "Commentary", "score": "Score"}),
                     hide_index=True, use_container_width=True, height=520)

# ---------------------------------------------------------------- momentum

with momentum_tab:
    shifts = data["analytics"].get("shifts", [])
    if data["balls"]:
        st.subheader("Worm, with momentum shifts")
        st.plotly_chart(worm_chart(data), use_container_width=True, config={"displayModeBar": False})
        st.caption("Highlighted stretches are detected momentum shifts, on the innings where they happened and tinted by the team they favoured; hover for detail. Dots are wickets.")
        st.subheader("Runs per over")
        st.plotly_chart(manhattan_chart(data), use_container_width=True, config={"displayModeBar": False})
        with st.expander("Data table"):
            st.dataframe(worm_frame(data), hide_index=True, use_container_width=True)
    st.subheader("Detected shifts")
    if not shifts:
        st.info("Momentum shifts need delivery-level data.")
    for shift in shifts:
        evidence = " · ".join(f"{item['label']}: {item['text']}" for item in (shift.get("evidence") or [])[:2] if item.get("text"))
        st.markdown(
            f"<div class='shift{' decisive' if shift.get('decisive') else ''}'><b>{SHIFT_LABELS[shift['type']]}{' · largest shift' if shift.get('decisive') else ''}</b> "
            f"— {shift['innings_team']} innings, overs {shift['from']}–{shift['to']}<br>{shift['headline']} · {shift['score_before']} → {shift['score_after']} · "
            f"favours <b>{shift['favours']}</b> ({shift['magnitude']} runs)<br><small>{evidence}</small></div>",
            unsafe_allow_html=True,
        )
    if shifts and st.button("Ask Pandit to explain the momentum"):
        st.session_state.setdefault(f"chat::{match['match_slug']}", [])
        result = agent().answer("Where did this match turn, and why?", ui_context={"series_slug": match["series_slug"], "match_slug": match["match_slug"], "teams": [t["abbr"] for t in match["teams"]]})
        st.info(result["reply"])
        st.caption(f"{result['provider']} · {result['model']}" if result.get("model_used") else "grounded recap (no LLM)")
