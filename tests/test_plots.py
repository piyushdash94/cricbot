"""
Tests for the optional image-chart layer.

matplotlib is an optional extra. These tests exercise the renderers when it
is present and verify the graceful path when it is not, so the suite passes
either way.

    pytest            # or: python tests/test_plots.py
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import scorecard_plots  # noqa: E402
from scorecard_plots import (  # noqa: E402
    PLOTS_AVAILABLE, MISSING_MESSAGE, plot_win_probability, plot_manhattan,
    plot_worm, plot_partnerships, plot_momentum, render_match_report,
)


def make_innings(team="RCB", n_overs=20):
    return {
        "team": {"abbreviation": team},
        "runs": 190, "wickets": 5, "overs": 20.0, "balls": 120,
        "inningOvers": [
            {"overNumber": n, "overRuns": 5 + (n % 7), "overWickets": n % 5 == 0,
             "totalRuns": 9 * n, "totalWickets": n // 5,
             "predictions": {"winProbability": 40.0 + n, "score": 190}}
            for n in range(1, n_overs + 1)
        ],
        "inningPartnerships": [
            {"runs": 95, "balls": 51, "player1Runs": 40, "player2Runs": 55,
             "player1": {"fieldingName": "Kohli"},
             "player2": {"fieldingName": "Salt"}},
            {"runs": 44, "balls": 23, "player1Runs": 30, "player2Runs": 14,
             "player1": {"fieldingName": "Kohli"},
             "player2": {"fieldingName": "Patidar"}},
        ],
    }


SCORECARD = {
    "match": {"statusText": "RCB won"},
    "content": {"innings": [make_innings("RCB"), make_innings("PBKS", 17)]},
}


def _tmpdir():
    return tempfile.mkdtemp(prefix="cricbot-plots-")


def _is_png(path):
    """A real PNG, not just a file that exists."""
    if not os.path.exists(path) or os.path.getsize(path) < 1000:
        return False
    with open(path, "rb") as fh:
        return fh.read(8) == b"\x89PNG\r\n\x1a\n"


# ------------------------------------------------- optional-extra contract


def test_module_imports_without_matplotlib():
    """Importing must never require the extra."""
    assert isinstance(PLOTS_AVAILABLE, bool)
    assert "matplotlib" in MISSING_MESSAGE


def test_core_toolkit_does_not_depend_on_plots():
    """The rest of the toolkit must not import matplotlib transitively."""
    import scorecard_renderer, cricbot_views, scorecard_charts  # noqa: F401
    for mod in (scorecard_renderer, cricbot_views, scorecard_charts):
        assert "matplotlib" not in open(mod.__file__).read()


def test_renderers_return_message_when_extra_missing(monkeypatch=None):
    """With matplotlib unavailable every renderer explains itself."""
    original = scorecard_plots._pyplot
    scorecard_plots._pyplot = lambda: None
    try:
        assert plot_manhattan([], "/tmp/x.png") == MISSING_MESSAGE
        assert plot_win_probability([], "T", "/tmp/x.png") == MISSING_MESSAGE
        assert plot_worm([], [], "/tmp/x.png") == MISSING_MESSAGE
        assert plot_partnerships([], "/tmp/x.png") == MISSING_MESSAGE
        assert plot_momentum([], "/tmp/x.png") == MISSING_MESSAGE
        assert render_match_report(SCORECARD, "/tmp") == MISSING_MESSAGE
    finally:
        scorecard_plots._pyplot = original


# ------------------------------------------------------------- rendering


def test_each_renderer_writes_a_png():
    if not PLOTS_AVAILABLE:
        print("  (skipped: matplotlib not installed)")
        return

    from scorecard_helpers import over_series, partnership_rows
    d = _tmpdir()
    try:
        inn = make_innings()
        assert _is_png(plot_manhattan(over_series(inn), f"{d}/m.png", "RCB"))
        assert _is_png(plot_win_probability([40, 55, 70], "RCB", f"{d}/w.png"))
        assert _is_png(plot_worm([[1, 5, 9], [2, 4, 8]], ["A", "B"], f"{d}/o.png"))
        assert _is_png(plot_partnerships(partnership_rows(inn), f"{d}/p.png"))
        assert _is_png(plot_momentum([-2.0, 0.5, 3.1, -1.0], f"{d}/mo.png"))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_none_values_do_not_crash_win_probability():
    """win_prob_series can contain None; it must break the line, not raise."""
    if not PLOTS_AVAILABLE:
        return
    d = _tmpdir()
    try:
        out = plot_win_probability([40, None, None, 80, 100], "RCB", f"{d}/w.png")
        assert _is_png(out)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_no_figures_leak():
    """A bot rendering per match must not accumulate open figures."""
    if not PLOTS_AVAILABLE:
        return
    import matplotlib.pyplot as plt
    d = _tmpdir()
    try:
        before = len(plt.get_fignums())
        render_match_report(SCORECARD, d)
        assert len(plt.get_fignums()) == before
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_render_match_report_writes_expected_set():
    if not PLOTS_AVAILABLE:
        return
    d = _tmpdir()
    try:
        written = render_match_report(SCORECARD, d)
        assert "win_probability" in written
        assert "worm" in written
        assert "manhattan_RCB" in written and "manhattan_PBKS" in written
        assert "partnerships_RCB" in written
        assert all(_is_png(p) for p in written.values())
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_render_match_report_with_momentum():
    if not PLOTS_AVAILABLE:
        return
    from impact_lab import momentum_series
    d = _tmpdir()
    try:
        written = render_match_report(SCORECARD, d, momentum_fn=momentum_series)
        assert "momentum_RCB" in written
        assert _is_png(written["momentum_RCB"])
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_render_match_report_empty_match():
    if not PLOTS_AVAILABLE:
        return
    assert render_match_report({"content": {"innings": []}}, "/tmp") == {}


def test_output_directory_created_if_absent():
    if not PLOTS_AVAILABLE:
        return
    d = _tmpdir()
    try:
        nested = os.path.join(d, "a", "b")
        assert _is_png(plot_momentum([1.0, -1.0], f"{nested}/m.png"))
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            passed += 1
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")

    note = "" if PLOTS_AVAILABLE else "  (matplotlib absent — render tests skipped)"
    print(f"\n{passed} passed, {failed} failed{note}")
    raise SystemExit(1 if failed else 0)
