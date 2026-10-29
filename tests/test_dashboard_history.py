from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest

from whatthefed.prediction_history import MACRO_WEIGHTS


def test_october_decision_has_ten_hold_votes_and_two_named_hike_dissents() -> None:
    html = (Path(__file__).resolve().parents[1] / "index.html").read_text(encoding="utf-8")
    october = html[html.index('label: "October 2026"'):html.index('label: "September 2026"')]
    assert 'official_vote: "10-2"' in october
    assert 'decision: "hold"' in october
    votes = re.search(r"votes: (\[.*?\])", october).group(1)
    assert json.loads(votes).count("hold") == 10
    assert json.loads(votes).count("raise") == 2
    assert "Lisa D. Cook and Beth M. Hammack" in october
    assert "monetary20261028a.htm" in october
    schedule = re.search(r"const FOMC_SCHEDULE = (\[.*?\]);", html, re.DOTALL).group(1)
    dates = re.findall(r'"(\d{4}-\d{2}-\d{2})"', schedule)
    assert [day for day in dates if day > "2026-10-28"] == [
        "2026-12-09", "2027-01-27", "2027-03-17", "2027-04-28",
        "2027-06-09", "2027-07-28", "2027-09-15", "2027-10-27", "2027-12-08",
    ]


def test_raw_data_source_links_are_the_first_column() -> None:
    html = (Path(__file__).resolve().parents[1] / "index.html").read_text(encoding="utf-8")
    start = html.index("    function drawRawDataTable()")
    end = html.index("    function initKgExperimentTabs()", start)
    table = html[start:end]
    assert re.findall(r"<th>(.*?)</th>", table) == [
        "Source Links", "Dataset", "As Of", "Metric", "ID", "Value", "Details",
    ]
    cells = re.findall(r"<td>(.*?)</td>", table, re.DOTALL)
    assert len(cells) == 7
    assert "row.sourceUrl" in cells[0]
    assert 'rel="noreferrer noopener"' in cells[0]
    assert '"n/a"' in cells[0]
    assert cells[1] == "${escHtml(row.dataset)}"


def test_meeting_evidence_is_inside_each_history_card_instead_of_a_separate_tab() -> None:
    html = (Path(__file__).resolve().parents[1] / "index.html").read_text(encoding="utf-8")
    start = html.index("    function renderTimeline()")
    end = html.index("    function renderTabs()", start)
    timeline = html[start:end]
    assert 'class="meeting-evidence-card"' in timeline
    assert 'data-meeting-date="${escHtml(meeting.meeting_date)}"' in timeline
    assert timeline.index("renderFullCard") < timeline.index('class="meeting-evidence-card"')
    assert timeline.index('class="meeting-evidence-card"') < timeline.index("meetingTimeline.appendChild")
    assert 'data-viz="meeting-evidence"' not in html
    assert 'id="viz-panel-meeting-evidence"' not in html
    assert 'id="evidence-graph"' not in html


def test_dual_mandate_is_first_and_experiment_links_match_their_numbers() -> None:
    html = (Path(__file__).resolve().parents[1] / "index.html").read_text(encoding="utf-8")
    tabs = re.findall(
        r'class="kg-tab[^"]*" data-kg-exp="([^"]+)">Experiment (\d+) · ([^<]+)</button>',
        html,
    )
    titles = [
        "Dual Mandate", "Bias Gradient", "Source Clusters", "Timeline Lanes",
        "Contribution Sunburst", "Decision Funnel", "Monte Carlo Cloud", "Sensitivity Tornado",
    ]
    assert tabs == [(f"experiment-{index}", str(index), title) for index, title in enumerate(titles, 1)]
    assert 'class="kg-tab kg-tab-active" data-kg-exp="experiment-1">Experiment 1 · Dual Mandate' in html
    cards = re.findall(
        r'<article class="experiment-details-card">\s*'
        r'<span class="experiment-number">Experiment (\d+)</span>\s*'
        r"<h3>([^<]+)</h3>.*?data-open-experiment=\"([^\"]+)\"",
        html, re.DOTALL,
    )
    assert cards == [(str(index), title, f"experiment-{index}") for index, title in enumerate(titles, 1)]
    assert sorted(re.findall(r"function buildGraphExperiment(\d+)\(", html)) == list("12345678")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
@pytest.mark.parametrize("labor_bias", [-1.0, -0.0931, 0.0, 1.0])
def test_dual_mandate_keeps_labor_prominent_without_changing_model_weights(labor_bias: float) -> None:
    html = (Path(__file__).resolve().parents[1] / "index.html").read_text(encoding="utf-8")
    start = html.index("      function buildGraphExperiment1(scene, context)")
    end = html.index("      // Shared model:", start)
    context = {
        "laborBias": labor_bias, "cpiBias": 0.2, "ppiBias": 1.0, "gdpBias": 0,
        "fiscalBias": 0, "policyRateBias": 0, "treasurySlopeBias": 0, "breakevenBias": 0,
    }
    script = """
const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
const importanceRadius = (importance, min, max) => min + clamp(importance, 0, 1) * (max - min);
const biasToColorHex = bias => bias;
const addEdge = () => {};
const addReferenceGrid = () => {};
function makeNode(scene, x, y, z, color, radius, label, sublabel, hover = true) {
  scene.push({ color, radius, label, sublabel, hover });
}
"""
    script += f"const MACRO_WEIGHTS = Object.freeze({json.dumps(MACRO_WEIGHTS)});\n"
    script += html[start:end]
    script += (
        f"const nodes = []; buildGraphExperiment1(nodes, {json.dumps(context)});\n"
        "console.log(JSON.stringify({nodes, weights: MACRO_WEIGHTS}));"
    )
    result = subprocess.run(
        [shutil.which("node"), "-e", script], check=True, capture_output=True, text=True,
    )
    payload = json.loads(result.stdout)
    labor = next(node for node in payload["nodes"] if "labor" in node["label"].lower())
    assert labor["radius"] == pytest.approx(0.38)
    assert labor["color"] == labor_bias
    assert labor["hover"] is False
    assert "21% weight" in labor["label"]
    assert payload["weights"] == MACRO_WEIGHTS
    if labor_bias < 0:
        assert "Cooler labor" in labor["label"]
        assert "less hike pressure" in labor["sublabel"]
    elif labor_bias > 0:
        assert "Hotter labor" in labor["label"]
        assert "more hike pressure" in labor["sublabel"]
    else:
        assert "Balanced labor" in labor["label"]
        assert "neutral pressure" in labor["sublabel"]
    if labor_bias == -0.0931:
        previous_radius = 0.10 + (0.42 - 0.10) * (0.21 * (0.20 + 0.80 * abs(labor_bias)) / 0.12)
        assert labor["radius"] > previous_radius


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
@pytest.mark.parametrize("archive_present", [False, True])
def test_recent_decisions_render_without_fred_and_merge_archive(archive_present: bool) -> None:
    html = (Path(__file__).resolve().parents[1] / "index.html").read_text()
    start = html.index('    const meetingTimeline = document.getElementById("meeting-timeline");')
    end = html.index("    // ── Data Visualization", start)
    meetings = [
        {
            "meeting_date": (date(2022, 1, 1) + timedelta(days=45 * index)).isoformat(),
            "label": f"Meeting {index}",
            "decision": "hold",
            "source_url": "https://www.federalreserve.gov/",
        }
        for index in range(36)
    ]
    recent = {}
    for meeting in meetings:
        recent.setdefault(meeting["meeting_date"][:4], []).append(meeting)
    archive = [
        {
            **meetings[0],
            "target_lower": 0.0,
            "target_upper": 0.25,
            "change_bps": 0,
        },
        {
            "meeting_date": "1990-01-01",
            "label": "Archive",
            "decision": "cut",
            "target_lower": 8.0,
            "target_upper": 8.0,
            "change_bps": -25,
        },
    ] if archive_present else []
    setup = """
const elements = {};
const document = {
  getElementById(id) {
    return elements[id] ||= {
      innerHTML: "", children: [],
      appendChild(child) { this.children.push(child); },
      querySelectorAll() { return []; },
    };
  },
  createElement() { return { innerHTML: "", className: "" }; },
};
const dashboardData = { meeting_history: [] };
const escHtml = value => String(value);
"""
    script = (
        setup
        + f"const window = {{ __FOMC_HISTORY_DATA__: {json.dumps(recent)} }};\n"
        + f"const fedRateHistoryOverlay = {{ meetings: {json.dumps(archive)} }};\n"
        + html[start:end]
        + """
console.log(JSON.stringify({
  meetings: allMeetings,
  rendered: meetingTimeline.children.length,
  missingRateCard: renderDbCard(allMeetings.find(m => m.label === "Meeting 1")).innerHTML,
  archivedRateCard: renderDbCard(allMeetings.find(m => m.label === "Meeting 0")).innerHTML,
}));
"""
    )
    result = subprocess.run(
        [shutil.which("node"), "-e", script], check=True, capture_output=True, text=True,
    )
    payload = json.loads(result.stdout)
    assert len(payload["meetings"]) == 36 + int(archive_present)
    assert len({meeting["meeting_date"] for meeting in payload["meetings"]}) == len(payload["meetings"])
    assert payload["rendered"] > 0
    assert "Target: Unavailable" in payload["missingRateCard"]
    assert "Rate change unavailable" in payload["missingRateCard"]
    assert "No rate change" not in payload["missingRateCard"]
    if archive_present:
        assert "0.00–0.25%" in payload["archivedRateCard"]
        assert "No rate change" in payload["archivedRateCard"]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_rate_chart_excludes_decisions_without_target_rates() -> None:
    html = (Path(__file__).resolve().parents[1] / "index.html").read_text()
    start = html.index("    const RATE_HISTORY = ")
    end = html.index("    const rateRangeControls", start)
    script = """
const allMeetings = [
  { meeting_date: "2026-01-01", decision: "hold" },
  { meeting_date: "2026-02-01", target_lower: null, target_upper: null },
  { meeting_date: "2026-03-01", target_lower: 0, target_upper: 0.25, decision: "cut" },
];
"""
    result = subprocess.run(
        [shutil.which("node"), "-e", script + html[start:end] + "console.log(JSON.stringify(RATE_HISTORY));"],
        check=True, capture_output=True, text=True,
    )
    assert json.loads(result.stdout) == [["2026-03-01", 0, 0.25, "cut"]]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
@pytest.mark.parametrize("meeting_count", [0, 1, 3])
def test_rate_chart_renders_recent_statement_rates_without_other_imports(meeting_count: int) -> None:
    html = (Path(__file__).resolve().parents[1] / "index.html").read_text()
    start = html.index('    const meetingTimeline = document.getElementById("meeting-timeline");')
    end = html.index("    function drawModelProbabilityHistory()", start)
    meetings = [
        {
            "meeting_date": f"2026-0{index + 1}-01",
            "label": f"Meeting {index}",
            "decision": "hold",
            "target_lower": 3.5,
            "target_upper": 3.75,
        }
        for index in reversed(range(meeting_count))
    ]
    script = """
const elements = {};
const document = {
  getElementById(id) {
    return elements[id] ||= {
      innerHTML: "", children: [],
      appendChild(child) { this.children.push(child); },
      querySelectorAll() { return []; },
    };
  },
  createElement() { return { innerHTML: "", className: "" }; },
  createElementNS(ns, tag) {
    return { tag, attrs: {}, setAttribute(k, v) { this.attrs[k] = v; } };
  },
};
const dashboardData = { meeting_history: [] };
const fedRateHistoryOverlay = null;
const escHtml = value => String(value);
"""
    script += f"const window = {{ __FOMC_HISTORY_DATA__: {{ '2026': {json.dumps(meetings)} }} }};\n"
    script += html[start:end] + """
drawRateTimeline();
console.log(JSON.stringify({ history: RATE_HISTORY, nodes: elements["rate-chart"].children }));
"""
    result = subprocess.run(
        [shutil.which("node"), "-e", script], check=True, capture_output=True, text=True,
    )
    payload = json.loads(result.stdout)
    assert len(payload["history"]) == meeting_count
    assert payload["history"] == sorted(payload["history"])
    assert payload["nodes"]
    assert "NaN" not in result.stdout
    assert "Infinity" not in result.stdout
    if meeting_count:
        assert any(node["tag"] == "path" for node in payload["nodes"])
    else:
        assert "Target rates unavailable" in result.stdout
