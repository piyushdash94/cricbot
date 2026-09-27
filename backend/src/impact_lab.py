"""Prototype logic for turning points, impact, and pitch profile."""

from .scorecard_helpers import (
    innings_team_name, over_series, short_name, player_name,
)

PACE_CODES = {"rf", "rfm", "rmf", "rm", "lf", "lfm", "lmf", "lm"}
SPIN_CODES = {"ob", "lb", "lbg", "sla", "slc", "rob", "lws"}


# ---------------------------------------------------------------- momentum

def turning_points(innings_list, top_n=3):
    """Overs with the largest swing in win probability."""
    events = []
    for inn in innings_list:
        team = innings_team_name(inn)
        rows = over_series(inn)
        prev = None
        for r in rows:
            wp = r["win_prob"]
            if wp is None:
                continue
            if prev is not None:
                events.append({
                    "team": team,
                    "over": r["over"],
                    "swing": wp - prev,
                    "win_prob": wp,
                    "runs": r["runs"],
                    "wickets": r["wickets"],
                })
            prev = wp

    events.sort(key=lambda e: abs(e["swing"]), reverse=True)
    return events[:top_n]


def momentum_series(inn, alpha=0.3, w_wkt=8.0, w_bdry=1.0, w_dot=0.5):
    """EWMA of per-over deviation from the innings' own par run rate."""
    rows = over_series(inn)
    if not rows:
        return []

    runs = [r["runs"] or 0 for r in rows]
    par = sum(runs) / len(runs)

    out, m = [], 0.0
    for r in rows:
        score = (r["runs"] or 0) - par - w_wkt * (r["wickets"] or 0)
        m = alpha * score + (1 - alpha) * m
        out.append(m)

    return out


# ------------------------------------------------------------------ impact

def match_par(innings_list):
    """Par strike rate and runs-per-ball derived from the match itself."""
    runs = balls = 0
    for inn in innings_list:
        runs += inn.get("runs") or 0
        balls += inn.get("balls") or 0
    if not balls:
        return None
    return {"par_sr": 100.0 * runs / balls, "par_rpb": runs / balls,
            "runs": runs, "balls": balls}


def batting_impact(innings_list, par, wicket_runs=12.0, entry_weight=3.0):
    """
    Runs above par for every batter.

    wicket_runs is the run-equivalent cost of losing a wicket, and it is the
    SAME constant credited to bowlers in bowling_impact(). Charging it here
    and crediting it there is what keeps the two scales comparable — see the
    zero-sum check in the lab notebook.
    """
    rows = []
    for inn in innings_list:
        total = inn.get("runs") or 1
        for b in inn.get("inningBatsmen", []):
            if b.get("battedType") == "sub":
                continue
            runs, balls = b.get("runs"), b.get("balls")
            if runs is None or not balls:
                continue

            above = runs - balls * par["par_rpb"]

            # Entry context: walking in early, with the innings unbuilt, is
            # harder than arriving at the death with a platform laid.
            entry = b.get("fowRuns")
            entry_bonus = 0.0
            if entry is not None and total and runs > 15:
                entry_bonus = entry_weight * (1 - entry / total)

            out = bool(b.get("isOut"))
            impact = above + entry_bonus - (wicket_runs if out else 0.0)

            rows.append({
                "player": player_name(b),
                "short": short_name(b.get("player") or {}),
                "team": innings_team_name(inn),
                "runs": runs, "balls": balls, "out": out,
                "impact": round(impact, 1),
            })
    return sorted(rows, key=lambda r: r["impact"], reverse=True)


def bowling_impact(innings_list, par, wicket_runs=12.0):
    """
    Runs saved plus wicket value, in the same units as batting impact.

    Uses the same wicket_runs constant that batting_impact() charges, so the
    two leaderboards are directly comparable and the match roughly sums to
    zero (extras and run-outs account for the residual).
    """
    rows = []
    for inn in innings_list:
        for b in inn.get("inningBowlers", []):
            balls = b.get("balls")
            conceded = b.get("conceded")
            if not balls or conceded is None:
                continue

            saved = balls * par["par_rpb"] - conceded
            wickets = b.get("wickets") or 0

            rows.append({
                "player": player_name(b),
                "short": short_name(b.get("player") or {}),
                "overs": b.get("overs"), "conceded": conceded,
                "wickets": wickets,
                "dots": b.get("dots"),
                "impact": round(saved + wickets * wicket_runs, 1),
            })
    return sorted(rows, key=lambda r: r["impact"], reverse=True)


def impact_leaderboard(innings_list, par, wicket_runs=12.0):
    """Batting and bowling impact merged onto one ranked scale."""
    bat = [dict(r, role="bat") for r in batting_impact(innings_list, par, wicket_runs)]
    bowl = [dict(r, role="bowl") for r in bowling_impact(innings_list, par, wicket_runs)]
    return sorted(bat + bowl, key=lambda r: r["impact"], reverse=True)


# ------------------------------------------------------------------- pitch

def bowler_kind(player):
    """Classify a bowler as pace or spin from bowlingStyles codes."""
    codes = {c.lower() for c in (player.get("bowlingStyles") or [])}
    if codes & SPIN_CODES:
        return "spin"
    if codes & PACE_CODES:
        return "pace"
    return "unknown"


def pitch_profile(innings_list):
    """Outcome-derived pitch signals for a single match."""
    kinds = {"pace": {"balls": 0, "runs": 0, "wkts": 0, "dots": 0},
             "spin": {"balls": 0, "runs": 0, "wkts": 0, "dots": 0}}
    dismissals = {}
    inn_rpo = []

    for inn in innings_list:
        balls = inn.get("balls") or 0
        runs = inn.get("runs") or 0
        if balls:
            inn_rpo.append(6.0 * runs / balls)

        for b in inn.get("inningBowlers", []):
            k = bowler_kind(b.get("player") or {})
            if k not in kinds:
                continue
            kinds[k]["balls"] += b.get("balls") or 0
            kinds[k]["runs"] += b.get("conceded") or 0
            kinds[k]["wkts"] += b.get("wickets") or 0
            kinds[k]["dots"] += b.get("dots") or 0

        for w in inn.get("inningWickets", []) or []:
            label = (w.get("dismissalText") or {})
            key = label.get("short") if isinstance(label, dict) else None
            dismissals[key or "other"] = dismissals.get(key or "other", 0) + 1

    def econ(d):
        return round(6.0 * d["runs"] / d["balls"], 2) if d["balls"] else None

    return {
        "pace": {**kinds["pace"], "econ": econ(kinds["pace"])},
        "spin": {**kinds["spin"], "econ": econ(kinds["spin"])},
        "spin_minus_pace_econ": (
            round(econ(kinds["spin"]) - econ(kinds["pace"]), 2)
            if econ(kinds["spin"]) is not None and econ(kinds["pace"]) is not None
            else None
        ),
        "innings_rpo": [round(x, 2) for x in inn_rpo],
        "deterioration": (
            round(inn_rpo[1] - inn_rpo[0], 2) if len(inn_rpo) >= 2 else None
        ),
        "dismissals": dismissals,
    }
