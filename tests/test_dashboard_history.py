from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest


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
