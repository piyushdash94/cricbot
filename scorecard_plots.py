"""
Image charts for cricket data.

The block-character charts in scorecard_charts.py are built for chat, where
they cost nothing and render anywhere. These are for when a real figure is
wanted — a full-resolution win-probability curve, a manhattan with axes.

matplotlib is an OPTIONAL extra (requirements-plots.txt). It is imported
lazily inside each renderer, so importing this module — or anything else in
the toolkit — works fine without it. Check PLOTS_AVAILABLE, or read the
message returned when a renderer is called without the extra installed.

Every renderer takes already-extracted data rather than raw API payloads, so
none of it needs the network.
"""

import os

from scorecard_helpers import (
    cumulative_runs,
    innings_team_name,
    over_series,
    partnership_rows,
    win_prob_series,
)

# One palette for the whole set, so the figures read as a system rather than
# as matplotlib defaults.
INK = "#1b1b1f"
MUTED = "#8a8f98"
GRID = "#e3e5e9"
TEAM_A = "#2f6fd0"
TEAM_B = "#d1495b"
ACCENT = "#e8a33d"
WICKET = "#d1495b"

MISSING_MESSAGE = (
    "matplotlib is not installed — install the optional extra with "
    "`pip install -r requirements-plots.txt`, or use scorecard_charts "
    "for text output."
)


def _pyplot():
    """
    Import pyplot on the Agg backend, or return None.

    Agg must be selected before pyplot is imported or this fails on any
    headless machine.
    """
    try:
        import matplotlib
    except ImportError:
        return None

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


PLOTS_AVAILABLE = _pyplot() is not None


def _style(ax, title=None, xlabel=None, ylabel=None):
    """Apply the shared look to an axis."""
    ax.set_facecolor("white")
    ax.grid(True, color=GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=9)

    if title:
        ax.set_title(title, color=INK, fontsize=12, pad=12, loc="left")
    if xlabel:
        ax.set_xlabel(xlabel, color=MUTED, fontsize=10)
    if ylabel:
        ax.set_ylabel(ylabel, color=MUTED, fontsize=10)


def _save(plt, fig, path):
    """Write the figure and always close it."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    try:
        fig.savefig(path, dpi=140, bbox_inches="tight", facecolor="white")
    finally:
        # Without this a long-running bot leaks a figure per render.
        plt.close(fig)
    return path


def plot_win_probability(series, team, path, innings_break=None):
    """
    Win-probability curve across a whole match.

    The x-axis is a match-wide sequence, not an innings over number — it
    runs past 20 in a T20 because both innings are concatenated. It is
    labelled accordingly, and innings_break draws the divider so the two
    halves are readable.

    Args:
        series: Percentages from win_prob_series(), which may contain None
        team: Team the curve tracks, for the title
        path: Output PNG path
        innings_break: Position where the second innings starts

    Returns:
        The path written, or MISSING_MESSAGE when matplotlib is absent
    """
    plt = _pyplot()
    if plt is None:
        return MISSING_MESSAGE

    # None must become nan so the line breaks at gaps instead of
    # interpolating straight through them.
    values = [float("nan") if v is None else v for v in series]
    x = range(1, len(values) + 1)

    fig, ax = plt.subplots(figsize=(9, 3.6))
    ax.axhline(50, color=MUTED, linewidth=1, linestyle="--", zorder=1)

    if innings_break and 0 < innings_break < len(values):
        ax.axvline(innings_break + 0.5, color=MUTED, linewidth=1.2,
                   linestyle=":", zorder=1)
        ax.text(innings_break + 0.5, 96, " innings break", color=MUTED,
                fontsize=9, ha="left", va="top")

    ax.plot(x, values, color=TEAM_A, linewidth=2.2, zorder=3)
    ax.fill_between(x, 50, values, color=TEAM_A, alpha=0.12, zorder=2)

    ax.set_ylim(0, 100)
    _style(ax, f"Win probability — {team}", "Over of match", "Win %")
    return _save(plt, fig, path)


def plot_manhattan(overs, path, team=""):
    """
    Runs per over as bars, wicket-taking overs highlighted.

    Args:
        overs: over_series() output
        path: Output PNG path
        team: Optional team name for the title

    Returns:
        The path written, or MISSING_MESSAGE
    """
    plt = _pyplot()
    if plt is None:
        return MISSING_MESSAGE

    numbers = [o.get("over") for o in overs]
    runs = [o.get("runs") or 0 for o in overs]
    colours = [
        WICKET if (o.get("wickets") or 0) else TEAM_A for o in overs
    ]

    fig, ax = plt.subplots(figsize=(9, 3.6))
    ax.bar(numbers, runs, color=colours, zorder=3, width=0.75)

    title = f"Runs per over — {team}" if team else "Runs per over"
    _style(ax, title, "Over", "Runs")

    # Overs are discrete — fractional ticks like "2.5" are meaningless.
    from matplotlib.ticker import MaxNLocator
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))

    handles = [
        plt.Rectangle((0, 0), 1, 1, color=TEAM_A),
        plt.Rectangle((0, 0), 1, 1, color=WICKET),
    ]
    ax.legend(handles, ["Runs", "Wicket fell"], frameon=False,
              fontsize=9, labelcolor=MUTED)
    return _save(plt, fig, path)


def plot_worm(innings_series, labels, path):
    """
    Cumulative runs, innings overlaid.

    Args:
        innings_series: List of cumulative-run sequences
        labels: Per-innings labels
        path: Output PNG path

    Returns:
        The path written, or MISSING_MESSAGE
    """
    plt = _pyplot()
    if plt is None:
        return MISSING_MESSAGE

    fig, ax = plt.subplots(figsize=(9, 3.6))
    for i, series in enumerate(innings_series):
        colour = TEAM_A if i == 0 else TEAM_B
        label = labels[i] if i < len(labels) else f"Innings {i + 1}"
        ax.plot(range(1, len(series) + 1), series, color=colour,
                linewidth=2.2, label=label, zorder=3)

    _style(ax, "Cumulative runs", "Over", "Runs")
    ax.legend(frameon=False, fontsize=9, labelcolor=MUTED)
    return _save(plt, fig, path)


def plot_partnerships(rows, path, top_n=6, team=""):
    """
    Biggest stands as stacked bars, split by each batter's contribution.

    Args:
        rows: partnership_rows() output
        path: Output PNG path
        top_n: Stands to show, largest first
        team: Optional team name for the title

    Returns:
        The path written, or MISSING_MESSAGE
    """
    plt = _pyplot()
    if plt is None:
        return MISSING_MESSAGE

    ranked = sorted(rows, key=lambda r: r.get("runs") or 0,
                    reverse=True)[:top_n]
    ranked.reverse()   # largest at the top of a horizontal chart

    labels = [f"{r['player1_short']}/{r['player2_short']}" for r in ranked]
    first = [r.get("player1_runs") or 0 for r in ranked]
    second = [r.get("player2_runs") or 0 for r in ranked]
    y = range(len(ranked))

    fig, ax = plt.subplots(figsize=(9, 0.55 * max(len(ranked), 3) + 1.4))
    ax.barh(y, first, color=TEAM_A, zorder=3, label="Batter 1")
    ax.barh(y, second, left=first, color=ACCENT, zorder=3, label="Batter 2")

    ax.set_yticks(list(y))
    ax.set_yticklabels(labels)
    title = f"Partnerships — {team}" if team else "Partnerships"
    _style(ax, title, "Runs")
    ax.legend(frameon=False, fontsize=9, labelcolor=MUTED)
    return _save(plt, fig, path)


def plot_momentum(series, path, team=""):
    """
    Momentum curve from impact_lab.momentum_series().

    Args:
        series: Momentum values
        path: Output PNG path
        team: Optional team name for the title

    Returns:
        The path written, or MISSING_MESSAGE
    """
    plt = _pyplot()
    if plt is None:
        return MISSING_MESSAGE

    x = range(1, len(series) + 1)

    fig, ax = plt.subplots(figsize=(9, 3.2))
    ax.axhline(0, color=MUTED, linewidth=1, zorder=1)
    ax.plot(x, series, color=INK, linewidth=2, zorder=3)
    ax.fill_between(x, 0, series, where=[v >= 0 for v in series],
                    color=TEAM_A, alpha=0.25, zorder=2, interpolate=True)
    ax.fill_between(x, 0, series, where=[v < 0 for v in series],
                    color=TEAM_B, alpha=0.25, zorder=2, interpolate=True)

    title = f"Momentum — {team}" if team else "Momentum"
    _style(ax, title, "Over", "Deviation from par")
    return _save(plt, fig, path)


def render_match_report(scorecard, out_dir, momentum_fn=None):
    """
    Write the full set of figures for a match.

    Args:
        scorecard: match_scorecard() response
        out_dir: Directory to write PNGs into
        momentum_fn: Optional impact_lab.momentum_series, passed in so this
            module does not depend on the experimental one

    Returns:
        Dict of chart name -> path, or MISSING_MESSAGE
    """
    plt = _pyplot()
    if plt is None:
        return MISSING_MESSAGE

    innings = scorecard.get("content", {}).get("innings", []) or []
    if not innings:
        return {}

    written = {}
    teams = [innings_team_name(inn) for inn in innings]

    for i, inn in enumerate(innings):
        team = teams[i]
        safe = str(team).replace("/", "-").replace(" ", "_")

        overs = over_series(inn)
        if overs:
            written[f"manhattan_{safe}"] = plot_manhattan(
                overs, os.path.join(out_dir, f"manhattan_{safe}.png"), team
            )

        stands = partnership_rows(inn)
        if stands:
            written[f"partnerships_{safe}"] = plot_partnerships(
                stands, os.path.join(out_dir, f"partnerships_{safe}.png"),
                team=team
            )

        if momentum_fn:
            momentum = momentum_fn(inn)
            if momentum:
                written[f"momentum_{safe}"] = plot_momentum(
                    momentum, os.path.join(out_dir, f"momentum_{safe}.png"),
                    team
                )

    if len(innings) >= 2:
        curve = win_prob_series(innings, teams[0])
        if any(v is not None for v in curve):
            written["win_probability"] = plot_win_probability(
                curve, teams[0], os.path.join(out_dir, "win_probability.png"),
                innings_break=len(over_series(innings[0])),
            )

        written["worm"] = plot_worm(
            [cumulative_runs(inn) for inn in innings], teams,
            os.path.join(out_dir, "worm.png")
        )

    return written
