import json
from datetime import date

import pytest

from whatthefed.backtest import HISTORY_GLOBALS
from whatthefed.meeting_evidence import (
    MeetingEvidenceUnavailable,
    build_evidence_graph,
    export_evidence_js,
    sync_and_read_graph,
)
from whatthefed.neo4j_connection import load_settings


def write_payload(path, global_name, payload):
    path.write_text(f"window.{global_name} = {json.dumps(payload)};\n", encoding="utf-8")


def evidence_inputs(path, *, input_date="2024-01-01", meeting_date="2024-03-20"):
    write_payload(path / "data_backtest_data.js", "__DATA_BACKTEST_DATA__", {
        "model_version": "data-v1",
        "meetings": [{
            "meeting_date": meeting_date, "actual": "hold", "predicted": "raise",
            "confidence": 0.55,
            "probabilities": {"raise": 0.55, "hold": 0.40, "cut": 0.05},
            "input_dates": {domain: input_date for domain in HISTORY_GLOBALS},
        }],
    })
    write_payload(path / "fed_rate_history_data.js", "__FED_RATE_HISTORY_DATA__", {
        "meetings": [{
            "meeting_date": meeting_date, "target_lower": 5.25, "target_upper": 5.50,
            "source_url": "https://www.federalreserve.gov/",
        }],
    })
    for filename, global_name, history_key, _ in HISTORY_GLOBALS.values():
        write_payload(path / filename, global_name, {
            history_key: [
                {"date": input_date, "bias": 0.1},
                {"date": "2099-01-01", "bias": -1.0},
            ],
        })


def test_graph_contains_only_selected_pre_meeting_observations(tmp_path):
    evidence_inputs(tmp_path)
    graph = build_evidence_graph(tmp_path, as_of=date(2024, 3, 21))
    assert len(graph["meetings"]) == 1
    assert len(graph["nodes"]) == 27
    assert len(graph["edges"]) == 26
    observations = [node for node in graph["nodes"] if node["kind"] == "observation"]
    assert len(observations) == 8
    assert all(node["observation_period"] == "2024-01-01" for node in observations)
    cpi = next(node for node in observations if node["domain"] == "cpi")
    assert cpi["estimated_available_on"] == "2024-02-20"
    assert all(node["estimated_available_on"] < "2024-03-20" for node in observations)
    assert graph == build_evidence_graph(tmp_path, as_of=date(2024, 3, 21))


def test_graph_rejects_unreleased_monthly_evidence(tmp_path):
    evidence_inputs(tmp_path, input_date="2024-03-01")
    with pytest.raises(ValueError, match="not eligible"):
        build_evidence_graph(tmp_path, as_of=date(2024, 3, 21))


def test_graph_excludes_future_meetings(tmp_path):
    evidence_inputs(tmp_path)
    with pytest.raises(MeetingEvidenceUnavailable, match="No completed meetings"):
        build_evidence_graph(tmp_path, as_of=date(2024, 2, 1))


def test_empty_backtest_reports_unavailable_before_loading_other_inputs(tmp_path):
    write_payload(tmp_path / "data_backtest_data.js", "__DATA_BACKTEST_DATA__", {"meetings": []})
    with pytest.raises(MeetingEvidenceUnavailable, match="refresh_fed_history and refresh_backtest"):
        build_evidence_graph(tmp_path)


def test_missing_observation_for_completed_meeting_is_not_treated_as_unavailable(tmp_path):
    evidence_inputs(tmp_path)
    filename, global_name, history_key, _ = HISTORY_GLOBALS["cpi"]
    write_payload(tmp_path / filename, global_name, {history_key: []})
    with pytest.raises(KeyError):
        build_evidence_graph(tmp_path, as_of=date(2024, 3, 21))


def test_export_contains_only_supplied_public_payload(tmp_path):
    path = tmp_path / "meeting_evidence_data.js"
    export_evidence_js({"available": False, "reason": "Not configured."}, path)
    assert path.read_text().startswith("window.__MEETING_EVIDENCE_DATA__ = ")
    assert "PASSWORD" not in path.read_text()


def test_settings_support_equals_in_password_and_environment_overrides(tmp_path, monkeypatch):
    for key in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD", "NEO4J_DATABASE"):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / ".env.neo4j"
    path.write_text(
        "NEO4J_URI=neo4j+s://example.databases.neo4j.io\n"
        "NEO4J_USERNAME=example\nNEO4J_PASSWORD=a=b=c\nNEO4J_DATABASE=example-db\n"
    )
    monkeypatch.setenv("NEO4J_DATABASE", "override-db")
    settings = load_settings(path)
    assert settings["NEO4J_PASSWORD"] == "a=b=c"
    assert settings["NEO4J_DATABASE"] == "override-db"


def test_settings_do_not_report_secret_values(tmp_path, monkeypatch):
    for key in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD", "NEO4J_DATABASE"):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / ".env.neo4j"
    path.write_text("NEO4J_PASSWORD=secret-value\n")
    with pytest.raises(ValueError) as error:
        load_settings(path)
    assert "secret-value" not in str(error.value)


class FakeResult:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def consume(self):
        return None

    def data(self):
        return self.rows


class FakeDriver:
    def __init__(self, graph, *, incomplete=False):
        self.graph = graph
        self.incomplete = incomplete
        self.queries = []

    def session(self, *, database):
        assert database == "test-db"
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute_write(self, callback):
        callback(self)

    def run(self, query, **parameters):
        self.queries.append((query, parameters))
        if "RETURN properties(n)" in query:
            nodes = [] if self.incomplete else self.graph["nodes"]
            return FakeResult([
                {"properties": {**node, "snapshot_id": parameters["snapshot_id"]}}
                for node in nodes
            ])
        if "RETURN a.id" in query:
            return FakeResult(self.graph["edges"])
        return FakeResult()


def test_neo4j_export_is_verified_and_does_not_expose_internal_properties(tmp_path):
    evidence_inputs(tmp_path)
    graph = build_evidence_graph(tmp_path, as_of=date(2024, 3, 21))
    driver = FakeDriver(graph)
    payload = sync_and_read_graph(driver, "test-db", graph)
    assert payload["backend"] == "Neo4j"
    assert payload["nodes"] == graph["nodes"]
    assert all("snapshot_id" not in node for node in payload["nodes"])
    again = sync_and_read_graph(driver, "test-db", graph)
    assert again["snapshot_id"] == payload["snapshot_id"]
    assert any("UNWIND $nodes" in query for query, _ in driver.queries)
    assert not any("DELETE" in query for query, _ in driver.queries)


def test_incomplete_neo4j_result_fails_instead_of_exporting_a_local_fallback(tmp_path):
    evidence_inputs(tmp_path)
    graph = build_evidence_graph(tmp_path, as_of=date(2024, 3, 21))
    with pytest.raises(RuntimeError, match="round-trip verification"):
        sync_and_read_graph(FakeDriver(graph, incomplete=True), "test-db", graph)
