"""
Text-mode charts for cricket data.

Every chart returns a plain string built from block characters, so it renders
inside a Discord/Telegram code fence with no image pipeline and no third-party
dependencies. Keep charts narrow: ~44 columns stays readable on mobile.
"""

BLOCKS = "▁▂▃▄▅▆▇█"

# Discord mobile shows roughly 56 monospace columns; leave room for labels.
DEFAULT_WIDTH = 44


def sparkline(values, lo=None, hi=None):
    """
    Render values as a single-line block sparkline.

    Args:
        values: Sequence of numbers (None entries render as a space)
        lo: Scale floor (default: min of values)
        hi: Scale ceiling (default: max of values)

    Returns:
        Sparkline string, one character per value
    """
    nums = [v for v in values if v is not None]
    if not nums:
        return ""

    lo = min(nums) if lo is None else lo
    hi = max(nums) if hi is None else hi
    span = hi - lo

    out = []
    for v in values:
        if v is None:
            out.append(" ")
            continue
        if span <= 0:
            out.append(BLOCKS[len(BLOCKS) // 2])
            continue
        frac = (v - lo) / span
        idx = round(frac * (len(BLOCKS) - 1))
        idx = max(0, min(len(BLOCKS) - 1, idx))
        out.append(BLOCKS[idx])

    return "".join(out)


def hbar(value, max_value, width=20, fill="█", empty="·"):
    """
    Render a single horizontal bar.

    Args:
        value: Bar value
        max_value: Value corresponding to a full-width bar
        width: Bar width in characters
        fill: Filled cell character
        empty: Empty cell character

    Returns:
        Bar string of exactly `width` characters
    """
    if not max_value or max_value <= 0 or value is None:
        return empty * width

    filled = int(round((value / max_value) * width))
    filled = max(0, min(width, filled))
    return fill * filled + empty * (width - filled)


def manhattan(overs, width=DEFAULT_WIDTH):
    """
    Runs-per-over sparkline with a wicket marker row beneath it.

    Args:
        overs: Sequence of over dicts (see scorecard_helpers.over_series)
        width: Maximum number of overs to show; longer innings are sampled

    Returns:
        Two-line string: runs sparkline, then wicket markers
    """
    if not overs:
        return "(no over data)"

    if len(overs) > width:
        step = len(overs) / width
        overs = [overs[int(i * step)] for i in range(width)]

    runs = [o.get("runs") or 0 for o in overs]
    line = sparkline(runs, lo=0)

    markers = "".join(
        "W" if (o.get("wickets") or 0) else "·" for o in overs
    )

    return f"{line}\n{markers}"


def win_probability(series, width=DEFAULT_WIDTH):
    """
    Win-probability curve on a fixed 0-100 scale.

    Args:
        series: Sequence of percentages for one team (see
            scorecard_helpers.win_prob_series)
        width: Maximum columns; longer series are sampled

    Returns:
        Sparkline string, or a placeholder when no data exists
    """
    pts = [p for p in series if p is not None]
    if not pts:
        return "(no win probability data)"

    if len(series) > width:
        step = len(series) / width
        series = [series[int(i * step)] for i in range(width)]

    # Fixed 0-100 scale: a flat 90% match should look flat, not full-range.
    return sparkline(series, lo=0, hi=100)


def worm(innings_series, labels=None, width=DEFAULT_WIDTH):
    """
    Cumulative-runs comparison, one sparkline per innings on a shared scale.

    Args:
        innings_series: List of per-innings cumulative run sequences
        labels: Optional per-innings labels
        width: Maximum columns per line

    Returns:
        One labelled line per innings
    """
    if not innings_series:
        return "(no data)"

    ceiling = max(
        (max(s) for s in innings_series if s),
        default=0,
    )

    lines = []
    for i, series in enumerate(innings_series):
        if len(series) > width:
            step = len(series) / width
            series = [series[int(j * step)] for j in range(width)]

        label = labels[i] if labels and i < len(labels) else f"Inn{i + 1}"
        lines.append(f"{label:<4} {sparkline(series, lo=0, hi=ceiling)}")

    return "\n".join(lines)


def _fit(text, size):
    """Pad or truncate text to exactly `size` characters."""
    if len(text) <= size:
        return text.ljust(size)
    return text[: max(0, size - 1)] + "…"


def partnership_bars(rows, top_n=5, width=24, label_width=16):
    """
    Horizontal bars for the biggest stands, split by each batter's contribution.

    The bar shows player1's share as '█' and player2's as '▓', so a lopsided
    partnership is visible at a glance.

    Args:
        rows: Partnership dicts (see scorecard_helpers.partnership_rows)
        top_n: Number of stands to show, largest first
        width: Bar width in characters
        label_width: Fixed width for the name column, so bars stay aligned
            however long the names are

    Returns:
        One line per partnership
    """
    if not rows:
        return "(no partnership data)"

    ranked = sorted(rows, key=lambda r: r.get("runs") or 0, reverse=True)[:top_n]
    ceiling = max((r.get("runs") or 0) for r in ranked) or 1

    lines = []
    for r in ranked:
        total = r.get("runs") or 0
        cells = int(round((total / ceiling) * width))

        p1 = r.get("player1_runs") or 0
        p2 = r.get("player2_runs") or 0
        share = p1 / (p1 + p2) if (p1 + p2) else 0.5

        p1_cells = int(round(cells * share))
        bar = "█" * p1_cells + "▓" * (cells - p1_cells)
        bar += "·" * (width - len(bar))

        pair = f"{r.get('player1_short', '?')}/{r.get('player2_short', '?')}"
        lines.append(
            f"{_fit(pair, label_width)}{bar} {total}({r.get('balls') or 0})"
        )

    return "\n".join(lines)


def phase_table(phases):
    """
    Powerplay / middle / death breakdown as aligned rows.

    Args:
        phases: Phase dicts (see scorecard_helpers.phase_splits)

    Returns:
        One line per phase
    """
    if not phases:
        return "(no phase data)"

    lines = []
    for p in phases:
        span = f"{p['start_over']}-{p['end_over']}"
        rpo = p["runs"] / max(1, p["end_over"] - p["start_over"] + 1)
        lines.append(
            f"{p['name']:<10}{span:>6}  "
            f"{p['runs']:>3}/{p['wickets']}  RPO {rpo:.1f}"
        )

    return "\n".join(lines)
