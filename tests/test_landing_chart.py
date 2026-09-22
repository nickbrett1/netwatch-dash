"""The landing page's loss chart, executed rather than asserted as text.

The chart is a string inside `landing.py`, so a Python test cannot call it and a
substring assertion cannot see geometry — which is exactly how it shipped with no
time axis, its lane labels drawn over the plot, and bars sixty times too wide.
`tests/js/loss_chart.mjs` runs the page's real script in a tiny DOM shim and
prints the SVG; these tests check that geometry.

Node is the only way to run it. Where node is absent the tests skip and say so,
rather than passing as if the chart had been checked.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from netwatch_dash.landing import LANDING_HTML

NODE = shutil.which("node")
HARNESS = Path(__file__).parent / "js" / "loss_chart.mjs"
PLOT_LEFT, PLOT_RIGHT = 150.0, 980.0


def _extract_script(tmp_path: Path) -> Path:
    match = re.search(r"<script>(.*)</script>", LANDING_HTML, re.DOTALL)
    assert match, "the landing page has no <script> block"
    path = tmp_path / "chart.js"
    path.write_text(match.group(1), encoding="utf-8")
    return path


def _run(tmp_path: Path, mode: str = "full") -> dict:
    if NODE is None:
        pytest.skip("node is not installed; the chart harness needs it")
    proc = subprocess.run(
        [NODE, str(HARNESS), str(_extract_script(tmp_path)), mode],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_the_loss_chart_labels_both_ends_of_its_time_axis(tmp_path):
    """Without an axis a lone bar cannot be placed in time — the original bug."""
    chart = _run(tmp_path)

    # The axis labels are the two texts on the bottom row, below every lane.
    bottom = max(t["y"] for t in chart["texts"])
    labels = [t for t in chart["texts"] if t["y"] == bottom]
    assert len(labels) == 2
    assert all(":" in t["s"] for t in labels)


def test_the_loss_lanes_share_one_left_origin(tmp_path):
    """Labels belong in a gutter; over the plot they ragged the axis's start."""
    chart = _run(tmp_path)

    baselines = [l for l in chart["lines"] if l["x1"] == PLOT_LEFT and l["x2"] == PLOT_RIGHT]
    assert len(baselines) == 4  # one per target
    assert {l["x1"] for l in baselines} == {PLOT_LEFT}


def test_a_loss_bar_is_as_wide_as_its_span_not_sixty_times_it(tmp_path):
    """`bucket_s` is seconds and the span is minutes: the width must divide by 60."""
    chart = _run(tmp_path)

    assert chart["rects"], "expected a bar for each span that lost a probe"
    plot_w = PLOT_RIGHT - PLOT_LEFT
    # Five rows one minute apart, so a one-minute bar is a quarter of the plot.
    # The unit bug drew it at 60/4 of the plot — far off the right edge.
    for bar in chart["rects"]:
        assert bar["w"] == pytest.approx(plot_w / 4)
        assert bar["w"] <= plot_w


def test_bar_height_is_the_share_lost_and_floors_so_one_probe_shows(tmp_path):
    """Height reads magnitude; a sub-pixel loss must not vanish."""
    chart = _run(tmp_path)
    bars = {b["title"].split()[0]: b for b in chart["rects"]}

    # gw lost all 12 probes in its minute -> full height (lane height - 10).
    assert bars["gw"]["h"] == pytest.approx(20.0)
    # net lost 4 of 12 -> a third of the lane.
    assert bars["net"]["h"] == pytest.approx(20.0 / 3)
    # wl lost 1 of 12 -> under a pixel, floored to 3 so it stays visible.
    assert bars["wl"]["h"] == pytest.approx(3.0)


def test_an_empty_window_says_so_instead_of_drawing_a_broken_frame(tmp_path):
    chart = _run(tmp_path, mode="empty")

    assert chart["rects"] == []
    assert any("no samples in the window" in t["s"] for t in chart["texts"])
    assert "nothing yet" in chart["note"]
