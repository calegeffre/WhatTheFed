from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="Node.js is unavailable")


def run_helpers(script: str):
    source = (ROOT / "assets" / "data-treemap.js").read_text(encoding="utf-8")
    helpers = source[source.index("  const finite"):source.index("  const element")]
    result = subprocess.run(
        [NODE, "-e", "const window = {};\n" + helpers + "\n" + script],
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)


def test_treemap_is_loaded_as_a_public_asset_with_an_explained_scale() -> None:
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    assert 'id="data-treemap"' in html
    assert 'src="assets/data-treemap.js?' in html
    assert 'id="heat-map"' not in html
    assert "1 favors lower rates, 3 is balanced, and 5 favors higher rates" in html
    assert "Rectangle sizes organize information, not forecast weights" in html


@requires_node
def test_policy_scale_is_monotonic_and_preserves_mild_cooling() -> None:
    scores = run_helpers(
        "console.log(JSON.stringify([-2,-1,-.5,-.0931,0,.35,.5,1,2].map(pressureScore)));"
    )
    assert scores == [1, 1, 2, 2.8, 3, 3.7, 4, 5, 5]
    assert scores == sorted(scores)


@requires_node
def test_missing_inputs_never_become_neutral_scores() -> None:
    payload = run_helpers("""
console.log(JSON.stringify({
  scores: [null,undefined,NaN,Infinity,"0"].map(pressureScore),
  cards: buildGroups().flatMap(group => group.cards).map(card => card.score),
  label: pressureLabel(null),
}));
""")
    assert payload["scores"] == [None] * 5
    assert all(score is None for score in payload["cards"])
    assert payload["label"] == "Data unavailable"


@requires_node
def test_markets_use_direction_not_confidence_for_the_policy_scale() -> None:
    payload = run_helpers("""
window.__MARKET_DASHBOARD_DATA__ = {
  target_meeting: "2026-10-28",
  providers: {
    kalshi: {probabilities: {raise: .07, hold: .91, cut: .02}},
    polymarket: {probabilities: {raise: 0, hold: 0, cut: 1}},
  },
};
console.log(JSON.stringify(buildGroups()[0].cards));
""")
    assert payload[0]["score"] == 3.1
    assert payload[0]["marketOutcome"] == "HOLD 91%"
    assert payload[1]["score"] == 1
    assert payload[1]["marketOutcome"] == "CUT 100%"


@requires_node
def test_reading_units_and_policy_context_are_preserved() -> None:
    payload = run_helpers("""
window.__CPI_DASHBOARD_DATA__ = {metrics: {cpi_bias:.35, core_yoy:.025}};
window.__PPI_DASHBOARD_DATA__ = {metrics: {ppi_bias:1, core_yoy:5}};
window.__LABOR_DASHBOARD_DATA__ = {metrics: {labor_bias:-.0931, unemployment_rate:4.1}};
window.__FOMC_DASHBOARD_DATA__ = {
  meeting_date:"2026-09-16", decision:"raise",
  signals:[{label:"Previous Meeting Bias",score:5,display:"+1.0"}],
};
console.log(JSON.stringify(buildGroups()));
""")
    assert [group["label"] for group in payload] == [
        "Market Data", "Inflation", "Labor", "Growth & Fiscal", "Rates & Funding", "Policy Context",
    ]
    assert payload[1]["cards"][0]["readings"][0][1] == "2.5%"
    assert payload[1]["cards"][1]["readings"][0][1] == "5%"
    assert payload[2]["cards"][0]["score"] == 2.8
    assert payload[2]["cards"][0]["readings"][1][1] == "4.1%"
    assert payload[5]["cards"][0]["label"] == "Previous decision"
    assert payload[5]["cards"][0]["score"] == 5


@requires_node
@pytest.mark.parametrize(("width", "height"), [(1100, 880), (320, 230), (1600, 300)])
def test_treemap_rectangles_fill_the_space_without_overlap(width: int, height: int) -> None:
    rectangles = run_helpers(f"""
const items = [20,26,18,12,12,12].map(weight => ({{weight}}));
console.log(JSON.stringify(layoutRectangles(items, {width}, {height})));
""")
    assert len(rectangles) == 6
    for index, rect in enumerate(rectangles):
        assert rect["x"] >= 0 and rect["y"] >= 0
        assert rect["x"] + rect["width"] <= width + 1e-8
        assert rect["y"] + rect["height"] <= height + 1e-8
        assert rect["width"] * rect["height"] == pytest.approx(
            width * height * rect["item"]["weight"] / 100
        )
        for other in rectangles[index + 1:]:
            overlap_x = min(rect["x"] + rect["width"], other["x"] + other["width"]) - max(rect["x"], other["x"])
            overlap_y = min(rect["y"] + rect["height"], other["y"] + other["height"]) - max(rect["y"], other["y"])
            assert overlap_x <= 1e-8 or overlap_y <= 1e-8
    assert sum(rect["width"] * rect["height"] for rect in rectangles) == pytest.approx(width * height)


@requires_node
def test_invalid_layout_dimensions_are_reported() -> None:
    result = run_helpers("""
let rejected = false;
try { layoutRectangles([{weight: 1}], 0, 100); } catch (error) { rejected = error instanceof RangeError; }
console.log(JSON.stringify(rejected));
""")
    assert result is True
