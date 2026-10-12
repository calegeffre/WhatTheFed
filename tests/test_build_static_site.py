from __future__ import annotations

from copy import deepcopy
import json
import runpy
import sqlite3
import subprocess
from urllib.error import HTTPError, URLError
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, Mock, call, patch

import pytest

from whatthefed.cpi_ingestion import CPIObservation, CPIStore
from whatthefed.prediction_history import (
    MACRO_WEIGHTS,
    PredictionHistoryError,
    build_prediction_snapshot,
    merge_history,
)


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "build-static-site.py"


def _script_namespace() -> dict[str, object]:
    return runpy.run_path(str(SCRIPT_PATH), run_name="build_static_site")


def test_ingestion_command_retries_then_succeeds() -> None:
    run_ingestion_command = _script_namespace()["run_ingestion_command"]
    failure = subprocess.CalledProcessError(1, ["python", "-m", "example"])

    with (
        patch("subprocess.run", side_effect=[failure, failure, None]) as run,
        patch("time.sleep") as sleep,
    ):
        run_ingestion_command(
            label="Example",
            module="example",
            arguments=["--value", "1"],
        )

    assert run.call_count == 3
    assert sleep.call_args_list == [call(10), call(30)]


def test_ingestion_command_raises_after_last_attempt() -> None:
    run_ingestion_command = _script_namespace()["run_ingestion_command"]
    failure = subprocess.CalledProcessError(1, ["python", "-m", "example"])

    with (
        patch("subprocess.run", side_effect=failure) as run,
        patch("time.sleep") as sleep,
    ):
        try:
            run_ingestion_command(
                label="Example",
                module="example",
                arguments=[],
            )
        except subprocess.CalledProcessError:
            pass
        else:
            raise AssertionError("Expected the final ingestion failure to be raised.")

    assert run.call_count == 3
    assert sleep.call_args_list == [call(10), call(30)]


def test_hung_ingestion_exhausts_shared_command_budget_without_more_retries() -> None:
    namespace = _script_namespace()
    run_command = namespace["run_ingestion_command"]
    timeout = subprocess.TimeoutExpired(["python", "-m", "example"], 180)
    with (
        patch("subprocess.run", side_effect=timeout) as run,
        patch("time.sleep") as sleep,
        patch("time.monotonic", side_effect=[0.0, 0.0, 180.0]),
    ):
        with pytest.raises(subprocess.TimeoutExpired):
            run_command(label="Example", module="example", arguments=[])
    run.assert_called_once()
    assert run.call_args.kwargs["timeout"] == 180.0
    sleep.assert_not_called()


def test_ingestion_retries_share_remaining_timeout_budget() -> None:
    namespace = _script_namespace()
    failure = subprocess.CalledProcessError(1, ["python", "-m", "example"])
    with (
        patch("subprocess.run", side_effect=[failure, None]) as run,
        patch("time.sleep"),
        patch("time.monotonic", side_effect=[0.0, 0.0, 40.0, 50.0]),
    ):
        namespace["run_ingestion_command"](label="Example", module="example", arguments=[])
    assert [item.kwargs["timeout"] for item in run.call_args_list] == [180.0, 130.0]


def test_existing_backtest_is_reused_without_recalculation() -> None:
    namespace = _script_namespace()
    payload = {"meetings": [{"meeting_date": "2026-09-16"}]}
    load_inputs = Mock()
    build_backtest = Mock()
    resolver_globals = namespace["resolve_backtest_payload"].__globals__

    with patch.dict(
        resolver_globals,
        {
            "load_backtest_inputs": load_inputs,
            "build_backtest_payload": build_backtest,
        },
    ):
        result = namespace["resolve_backtest_payload"](
            Path("unused"),
            payload,
            as_of=date(2026, 10, 8),
        )

    assert result is payload
    load_inputs.assert_not_called()
    build_backtest.assert_not_called()


def test_backtest_filters_missing_or_invalid_target_ranges() -> None:
    resolver = _script_namespace()["resolve_backtest_payload"]
    valid = [
        {"meeting_date": "2024-01-01", "target_lower": 0.0, "target_upper": 0.25},
        {"meeting_date": "2024-02-01", "target_lower": 3.5, "target_upper": 3.75},
    ]
    invalid = [
        {"meeting_date": "2024-03-01"},
        {"meeting_date": "2024-04-01", "target_lower": None, "target_upper": 3.75},
        {"meeting_date": "2024-05-01", "target_lower": 3.5, "target_upper": float("nan")},
        {"meeting_date": "2024-06-01", "target_lower": float("inf"), "target_upper": 3.75},
        {"meeting_date": "2024-07-01", "target_lower": 4.0, "target_upper": 3.75},
        {"meeting_date": "2024-08-01", "target_lower": False, "target_upper": 0.25},
    ]
    build = Mock(return_value={"meetings": []})
    with patch.dict(resolver.__globals__, {
        "load_backtest_inputs": Mock(return_value=(valid + invalid, {})),
        "build_backtest_payload": build,
    }):
        resolver(Path("unused"), None, as_of=date(2024, 9, 1))
    assert build.call_args.kwargs["meetings"] == valid


def test_backtest_still_calculates_valid_archive_when_legacy_recent_rows_lack_rates() -> None:
    resolver = _script_namespace()["resolve_backtest_payload"]
    meetings = [
        {"meeting_date": "2023-12-13", "decision": "hold", "target_lower": 5.25, "target_upper": 5.5},
        {"meeting_date": "2024-03-20", "decision": "hold", "target_lower": 5.25, "target_upper": 5.5},
        {"meeting_date": "2024-06-12", "decision": "raise", "target_lower": 5.5, "target_upper": 5.75},
        {"meeting_date": "2024-07-31", "decision": "hold"},
    ]
    histories = {
        key: [{"date": "2023-10-01", "bias": 0.0}] for key in MACRO_WEIGHTS
    }
    with patch.dict(resolver.__globals__, {
        "load_backtest_inputs": Mock(return_value=(meetings, histories)),
    }):
        payload = resolver(Path("unused"), None, as_of=date(2024, 8, 1))
    assert payload["meeting_count"] == 2
    assert payload["coverage_end"] == "2024-06-12"


def test_fetch_existing_backtest_parses_deployed_payload() -> None:
    namespace = _script_namespace()
    payload_text = (
        'window.__DATA_BACKTEST_DATA__ = '
        '{"meetings":[{"meeting_date":"2026-09-16"}]};'
    )
    response = MagicMock()
    response.__enter__.return_value.read.return_value = payload_text.encode()
    urlopen = Mock(return_value=response)
    fetch_globals = namespace["fetch_existing_backtest"].__globals__

    with patch.dict(fetch_globals, {"urlopen": urlopen}):
        payload = namespace["fetch_existing_backtest"](
            "https://example.test/data_backtest_data.js"
        )

    assert payload == {"meetings": [{"meeting_date": "2026-09-16"}]}
    urlopen.assert_called_once()


@pytest.mark.parametrize("status", [404, 500])
def test_fetch_existing_fed_history_http_errors(status: int) -> None:
    namespace = _script_namespace()
    fetch = namespace["fetch_existing_meetings"]
    error = HTTPError("https://example.test/history.js", status, "error", {}, None)
    with patch.dict(fetch.__globals__, {"urlopen": Mock(side_effect=error)}):
        if status == 404:
            assert fetch(error.url, "__FED_RATE_HISTORY_DATA__") is None
        else:
            with pytest.raises(HTTPError):
                fetch(error.url, "__FED_RATE_HISTORY_DATA__")


@pytest.mark.parametrize("text", [
    'window.__FED_RATE_HISTORY_DATA__ = {"meetings": []};',
    'window.__WRONG_DATA__ = {"meetings": []};',
    'window.__FED_RATE_HISTORY_DATA__ = {"meetings": null};',
])
def test_fetch_existing_fed_history_validates_payload(text: str) -> None:
    namespace = _script_namespace()
    fetch = namespace["fetch_existing_meetings"]
    response = MagicMock()
    response.__enter__.return_value.read.return_value = text.encode()
    with patch.dict(fetch.__globals__, {"urlopen": Mock(return_value=response)}):
        if "__WRONG_DATA__" in text or "null" in text:
            with pytest.raises(RuntimeError):
                fetch("https://example.test/history.js", "__FED_RATE_HISTORY_DATA__")
        else:
            assert fetch("https://example.test/history.js", "__FED_RATE_HISTORY_DATA__") == {
                "meetings": []
            }


@pytest.mark.parametrize(
    ("cached_history", "cached_backtest", "refresh"),
    [
        ({"meetings": [{"meeting_date": "2026-09-16"}]}, {"meetings": []}, False),
        (None, None, False),
        ({"meetings": []}, None, False),
        (None, {"meetings": [{"meeting_date": "2026-09-16"}]}, False),
        (None, None, True),
    ],
)
def test_build_site_only_fetches_fred_on_explicit_refresh(
    tmp_path: Path,
    cached_history: dict[str, object] | None,
    cached_backtest: dict[str, object] | None,
    refresh: bool,
) -> None:
    namespace = _script_namespace()
    build_site = namespace["build_site"]
    # Use a disposable repository root so the complete artifact can be checked.
    (tmp_path / "index.html").write_text("<html></html>")
    output_dir = tmp_path / "_site"
    fresh_history = {"meetings": [{
        "meeting_date": "2026-09-16", "target_lower": 3.5, "target_upper": 3.75,
    }]}
    fetch_fred = Mock(return_value=fresh_history)
    fetch_cached = Mock(return_value=cached_history)
    resolver = Mock(return_value=cached_backtest or {"meetings": []})

    def ingest(*, label: str, module: str, arguments: list[str]) -> None:
        _write_source_exports(namespace, arguments, {"fomc_history_data.js": {"2026": []}})

    with patch.dict(build_site.__globals__, {
        "REPO_ROOT": tmp_path,
        "fetch_existing_history": Mock(return_value={"snapshots": []}),
        "fetch_existing_backtest": Mock(return_value=cached_backtest),
        "fetch_existing_meetings": fetch_cached,
        "fetch_history_payload": fetch_fred,
        "run_ingestion_command": ingest,
        "resolve_backtest_payload": resolver,
    }):
        build_site(
            output_dir=output_dir,
            fed_history_source_url="https://example.test/history.js",
            refresh_fed_history=refresh,
        )

    if refresh:
        fetch_fred.assert_called_once_with(start_year=1982)
    else:
        fetch_fred.assert_not_called()
    fetch_cached.assert_called_once()
    if cached_backtest is None and ((cached_history and cached_history["meetings"]) or refresh):
        resolver.assert_called_once()
    else:
        resolver.assert_not_called()
    text = (output_dir / "data" / "fed_rate_history_data.js").read_text()
    payload = json.loads(text.split(" = ", 1)[1].rstrip(";\n"))
    assert payload["meetings"] == (
        fresh_history if refresh else cached_history or {"meetings": []}
    )["meetings"]
    assert all((output_dir / "data" / name).is_file() for name in namespace["EXPECTED_PAYLOADS"])
    assert (output_dir / "index.html").is_file()
    assert not list(output_dir.rglob("*.db"))
    assert len(namespace["read_payload"](
        output_dir / "data/model_probability_history_data.js"
    )["snapshots"]) == 1
    status = namespace["read_payload"](output_dir / "data/refresh_status_data.js")
    assert status["failures"] == []
    assert datetime.fromisoformat(status["generated_at"]).tzinfo is not None


@pytest.mark.parametrize("with_archive", [False, True])
def test_explicit_fed_history_refresh_failure_completes_build(
    tmp_path: Path, with_archive: bool, capsys
) -> None:
    namespace = _script_namespace()
    build_site = namespace["build_site"]
    (tmp_path / "index.html").write_text("<html></html>")
    cache = tmp_path / ".refresh-cache"
    archive = {"meetings": [{"meeting_date": "1982-01-01", "decision": "hold"}]}
    if with_archive:
        namespace["write_payload"](archive, cache / "data" / "fed_rate_history_data.js")
    ingested = []

    def ingest(*, label: str, module: str, arguments: list[str]) -> None:
        ingested.append(module)
        _write_source_exports(namespace, arguments, {"fomc_history_data.js": {"2026": []}})

    def fail_fred(**kwargs):
        assert len(ingested) == 11
        assert (cache / "data" / "cpi_dashboard_data.js").is_file()
        raise TimeoutError("FRED timed out")

    with patch.dict(build_site.__globals__, {
        "REPO_ROOT": tmp_path,
        "run_ingestion_command": ingest,
        "fetch_history_payload": fail_fred,
        "resolve_backtest_payload": Mock(return_value={"meetings": []}),
    }):
        build_site(output_dir=tmp_path / "_site", cache_dir=cache, refresh_fed_history=True)
    assert namespace["read_payload"](tmp_path / "_site/data/fed_rate_history_data.js")[
        "meetings"
    ] == (archive["meetings"] if with_archive else [])
    assert all((tmp_path / "_site/data" / name).is_file() for name in namespace["EXPECTED_PAYLOADS"])
    assert "FRED timed out" in capsys.readouterr().out
    status = namespace["read_payload"](tmp_path / "_site/data/refresh_status_data.js")
    assert "Fed rate history" in status["failures"]


def test_cli_passes_fed_history_options() -> None:
    main = _script_namespace()["main"]
    build_site = Mock()
    with patch.dict(main.__globals__, {"build_site": build_site}):
        assert main([
            "--fed-history-source-url", "https://example.test/history.js",
            "--refresh-fed-history",
            "--cache-dir", ".refresh-cache",
        ]) == 0
    assert build_site.call_args.kwargs["refresh_fed_history"] is True
    assert build_site.call_args.kwargs["fed_history_source_url"] == "https://example.test/history.js"
    assert build_site.call_args.kwargs["cache_dir"] == Path(".refresh-cache")


def _complete_payloads() -> dict[str, object]:
    return {
        "market": {
            "target_meeting": "2026-09-16",
            "providers": {
                "kalshi": {"probabilities": {"raise": 0.6, "hold": 0.35, "cut": 0.05}},
                "polymarket": {"probabilities": {"raise": 0.5, "hold": 0.45, "cut": 0.05}},
            },
            "blended_probabilities": {"raise": 0.55, "hold": 0.4, "cut": 0.05},
        },
        "fomc": {
            "decision": "raise",
            "meeting_date": "2026-07-29",
            "signals": [{"label": "Previous Meeting Bias", "display": "+0.25"}],
        },
        "cpi": {"metrics": {"cpi_bias": 0.2}},
        "ppi": {"metrics": {"ppi_bias": 0.4}},
        "labor": {"metrics": {"labor_bias": 0.1}},
        "gdp": {"metrics": {"gdp_bias": -0.2}},
        "breakeven": {"metrics": {"breakeven_bias": 0.05}},
        "treasury": {
            "points": [
                {"maturity": "2Y", "yield_pct": 3.8},
                {"maturity": "10Y", "yield_pct": 4.1},
            ]
        },
        "policy_rate": {"metrics": {"policy_rate_bias": -0.1}},
        "fiscal": {"metrics": {"fiscal_bias": 0.3}},
    }


def _write_source_exports(namespace, arguments, overrides=None) -> None:
    payloads = {
        filename: _complete_payloads()[key]
        for key, (filename, _) in namespace["PAYLOAD_GLOBALS"].items()
    }
    payloads.update({
        "fomc_history_data.js": {
            "2026": [{"meeting_date": "2026-07-29", "decision": "raise"}]
        },
        "kg_dashboard_data.js": {"cpi_graph": {"nodes": [], "edges": []}},
        "labor_kg_dashboard_data.js": {"labor_graph": {"nodes": [], "edges": []}},
        "fed_events_dashboard_data.js": {"events": []},
    })
    payloads.update(overrides or {})
    for index, argument in enumerate(arguments):
        if argument in namespace["EXPORT_OPTIONS"]:
            path = Path(arguments[index + 1])
            namespace["write_payload"](payloads[path.name], path)


def test_cache_survives_multiple_runs_and_failed_partial_source(tmp_path: Path, capsys) -> None:
    namespace = _script_namespace()
    build_site = namespace["build_site"]
    cache = tmp_path / ".refresh-cache"
    output = tmp_path / "_site"
    (tmp_path / "index.html").write_text("<html></html>")
    round_number = 0
    db_paths = []

    def ingest(*, label: str, module: str, arguments: list[str]) -> None:
        if "--db-path" in arguments:
            db_path = Path(arguments[arguments.index("--db-path") + 1])
            db_paths.append(db_path)
            with sqlite3.connect(db_path) as connection:
                connection.execute("CREATE TABLE IF NOT EXISTS observations (source TEXT)")
                if module.endswith("cpi_ingestion") and round_number == 1:
                    assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] >= 10
                connection.execute("INSERT INTO observations VALUES (?)", (module,))
        if round_number == 1 and module.endswith("cpi_ingestion"):
            path = Path(arguments[arguments.index("--dashboard-js") + 1])
            namespace["write_payload"]({"metrics": {"cpi_bias": 0.9}}, path)
            raise subprocess.CalledProcessError(1, ["cpi"])
        overrides = {"ppi_dashboard_data.js": {"metrics": {"ppi_bias": 0.7}}} if round_number else {}
        _write_source_exports(namespace, arguments, overrides)

    snapshot = Mock(wraps=build_prediction_snapshot)
    remote = Mock(side_effect=[{"snapshots": []}, None, None])
    with patch.dict(build_site.__globals__, {
        "REPO_ROOT": tmp_path,
        "run_ingestion_command": ingest,
        "fetch_existing_history": remote,
        "fetch_existing_backtest": remote,
        "fetch_existing_meetings": remote,
        "resolve_backtest_payload": Mock(return_value={"meetings": []}),
        "build_prediction_snapshot": snapshot,
    }):
        build_site(output_dir=output, cache_dir=cache)
        saved_cpi = (cache / "data/cpi_dashboard_data.js").read_text()
        saved_history = (cache / "data/model_probability_history_data.js").read_text()
        round_number = 1
        build_site(output_dir=output, cache_dir=cache)

    assert set(db_paths) == {cache / "market_snapshots.db"}
    assert not list(output.rglob("*.db"))
    assert (output / "data/cpi_dashboard_data.js").read_text() == saved_cpi
    assert (cache / "data/cpi_dashboard_data.js").read_text() == saved_cpi
    assert (cache / "data/model_probability_history_data.js").read_text() == saved_history
    assert namespace["read_payload"](cache / "data/ppi_dashboard_data.js")["metrics"]["ppi_bias"] == 0.7
    assert snapshot.call_count == 1
    assert remote.call_count == 3
    assert "Required sources were not all refreshed successfully" in capsys.readouterr().out
    status = namespace["read_payload"](output / "data/refresh_status_data.js")
    assert status["failures"] == ["CPI", "Model snapshot"]
    assert not (cache / "data/refresh_status_data.js").exists()


@pytest.mark.parametrize("bad_export", ["missing", "malformed", "null", "incomplete", "wrong_global"])
def test_partial_source_outputs_never_replace_cache(tmp_path: Path, bad_export: str) -> None:
    namespace = _script_namespace()
    build_site = namespace["build_site"]
    (tmp_path / "index.html").write_text("<html></html>")
    cache = tmp_path / ".refresh-cache"
    cpi = {"metrics": {"cpi_bias": 0.2}}
    kg = {"cpi_graph": {"nodes": [], "edges": []}}
    namespace["write_payload"](cpi, cache / "data/cpi_dashboard_data.js")
    namespace["write_payload"](kg, cache / "data/kg_dashboard_data.js")

    def ingest(*, label: str, module: str, arguments: list[str]) -> None:
        _write_source_exports(namespace, arguments)
        if module.endswith("cpi_ingestion"):
            dashboard = Path(arguments[arguments.index("--dashboard-js") + 1])
            namespace["write_payload"]({"metrics": {"cpi_bias": 0.9}}, dashboard)
            graph = Path(arguments[arguments.index("--kg-js") + 1])
            if bad_export == "missing":
                graph.unlink()
            else:
                graph.write_text({
                    "malformed": "window.__KG_DASHBOARD_DATA__ = {",
                    "null": "window.__KG_DASHBOARD_DATA__ = null;",
                    "incomplete": "window.__KG_DASHBOARD_DATA__ = {};",
                    "wrong_global": "window.__WRONG_DATA__ = {};",
                }[bad_export])

    with patch.dict(build_site.__globals__, {
        "REPO_ROOT": tmp_path, "run_ingestion_command": ingest,
        "resolve_backtest_payload": Mock(return_value={"meetings": []}),
    }):
        build_site(output_dir=tmp_path / "_site", cache_dir=cache)
    for directory in (cache, tmp_path / "_site"):
        assert namespace["read_payload"](directory / "data/cpi_dashboard_data.js") == cpi
        assert namespace["read_payload"](directory / "data/kg_dashboard_data.js") == kg
    assert namespace["read_payload"](cache / "data/model_probability_history_data.js")["snapshots"] == []


def test_all_missing_sources_export_null_and_keep_building(tmp_path: Path, capsys) -> None:
    namespace = _script_namespace()
    build_site = namespace["build_site"]
    (tmp_path / "index.html").write_text("<html></html>")
    ingestion = Mock(side_effect=TimeoutError("source unavailable"))
    with patch.dict(build_site.__globals__, {"REPO_ROOT": tmp_path, "run_ingestion_command": ingestion}):
        build_site(output_dir=tmp_path / "_site")
    assert ingestion.call_count == 11
    for name, global_name in namespace["PAYLOAD_NAMES"].items():
        text = (tmp_path / "_site/data" / name).read_text()
        if name not in {
            "fed_rate_history_data.js", "data_backtest_data.js", "model_probability_history_data.js",
            "refresh_status_data.js",
        }:
            assert text == f"window.{global_name} = null;\n"
    assert "Model snapshot skipped" in capsys.readouterr().out
    assert not list(tmp_path.rglob("*.db"))
    status = namespace["read_payload"](tmp_path / "_site/data/refresh_status_data.js")
    assert len(status["failures"]) == 12
    assert status["failures"][0] == "FOMC statements"
    assert "CPI" in status["failures"]


@pytest.mark.parametrize("with_cache", [False, True])
def test_failed_model_and_backtest_preserve_archives(tmp_path: Path, with_cache: bool, capsys) -> None:
    namespace = _script_namespace()
    build_site = namespace["build_site"]
    (tmp_path / "index.html").write_text("<html></html>")
    cache = tmp_path / ".refresh-cache" if with_cache else None
    history = {"snapshots": [{"captured_at": "2026-01-01", "target_meeting": "2026-09-16"}]}
    backtest = {"meetings": [{"meeting_date": "2026-01-01", "decision": "hold"}]}
    if cache:
        namespace["write_payload"](history, cache / "data/model_probability_history_data.js")
        namespace["write_payload"](backtest, cache / "data/data_backtest_data.js")
    remote = Mock(side_effect=TimeoutError("history unavailable"))
    with patch.dict(build_site.__globals__, {
        "REPO_ROOT": tmp_path,
        "run_ingestion_command": lambda **kwargs: _write_source_exports(namespace, kwargs["arguments"]),
        "fetch_existing_history": remote if cache else Mock(return_value={"snapshots": []}),
        "fetch_existing_backtest": remote if cache else Mock(return_value=None),
        "fetch_existing_meetings": Mock(return_value={"meetings": [{
            "meeting_date": "2026-01-01", "decision": "hold",
            "target_lower": 3.5, "target_upper": 3.75,
        }]}),
        "build_prediction_snapshot": Mock(side_effect=PredictionHistoryError("incomplete model")),
        "resolve_backtest_payload": Mock(side_effect=RuntimeError("backtest failed")),
    }):
        build_site(
            output_dir=tmp_path / "_site", cache_dir=cache,
            history_source_url="https://example.test/history.js", refresh_backtest=True,
        )
    assert namespace["read_payload"](tmp_path / "_site/data/model_probability_history_data.js") == (
        history if cache else {"snapshots": []}
    )
    assert namespace["read_payload"](tmp_path / "_site/data/data_backtest_data.js") == (
        backtest if cache else {"meetings": []}
    )
    assert "incomplete model" in capsys.readouterr().out
    status = namespace["read_payload"](tmp_path / "_site/data/refresh_status_data.js")
    assert "Backtest" in status["failures"]
    assert "Model snapshot" in status["failures"]


def test_fomc_decisions_are_preserved_separately_from_rate_archive_across_runs(tmp_path: Path) -> None:
    namespace = _script_namespace()
    build_site = namespace["build_site"]
    (tmp_path / "index.html").write_text("<html></html>")
    cache = tmp_path / ".refresh-cache"
    recent = [
        {"meeting_date": date(2024 + index // 12, index % 12 + 1, 1).isoformat(), "decision": "hold"}
        for index in range(36)
    ]
    grouped = {}
    for item in recent:
        grouped.setdefault(item["meeting_date"][:4], []).append(item)
    archive = {"meetings": [{
        "meeting_date": "1982-01-01", "decision": "raise", "target_lower": 12.0, "target_upper": 12.0,
    }]}
    namespace["write_payload"](archive, cache / "data/fed_rate_history_data.js")
    current = grouped

    def ingest(**kwargs):
        _write_source_exports(namespace, kwargs["arguments"], {"fomc_history_data.js": current})

    fred = Mock(return_value={"meetings": [{
        "meeting_date": recent[-1]["meeting_date"], "target_lower": 3.5, "target_upper": 3.75,
        "decision": "cut",
    }]})
    with patch.dict(build_site.__globals__, {
        "REPO_ROOT": tmp_path, "run_ingestion_command": ingest,
        "fetch_history_payload": fred,
        "resolve_backtest_payload": Mock(return_value={"meetings": []}),
    }):
        build_site(output_dir=tmp_path / "_site", cache_dir=cache, refresh_fed_history=True)
        current = {"2026": [recent[-1]]}
        build_site(output_dir=tmp_path / "_site", cache_dir=cache, refresh_fed_history=True)
    history = namespace["read_payload"](cache / "data/fed_rate_history_data.js")
    assert len(history["meetings"]) == 2
    assert len({item["meeting_date"] for item in history["meetings"]}) == 2
    assert history["meetings"][0]["target_lower"] == 12.0
    assert history["meetings"][-1]["target_upper"] == 3.75
    assert history["meetings"][-1]["decision"] == "cut"
    fomc = namespace["read_payload"](cache / "data/fomc_history_data.js")
    assert sum(len(group) for group in fomc.values()) == 36


def test_successful_sources_remain_cached_after_later_build_failure(tmp_path: Path) -> None:
    namespace = _script_namespace()
    build_site = namespace["build_site"]
    cache = tmp_path / ".refresh-cache"
    with patch.dict(build_site.__globals__, {
        "REPO_ROOT": tmp_path,
        "run_ingestion_command": lambda **kwargs: _write_source_exports(namespace, kwargs["arguments"]),
        "resolve_backtest_payload": Mock(return_value={"meetings": []}),
    }):
        with pytest.raises(FileNotFoundError):
            build_site(output_dir=tmp_path / "_site", cache_dir=cache)
    assert namespace["read_payload"](cache / "data/cpi_dashboard_data.js")["metrics"]["cpi_bias"] == 0.2
    assert len(namespace["read_payload"](cache / "data/model_probability_history_data.js")["snapshots"]) == 1


def test_retry_clears_exports_from_failed_attempt(tmp_path: Path) -> None:
    namespace = _script_namespace()
    path = tmp_path / "cpi_dashboard_data.js"
    run_command = namespace["run_ingestion_command"]

    def run(*args, **kwargs):
        if not run_mock.call_count == 2:
            path.write_text("partial export")
            raise subprocess.CalledProcessError(1, ["example"])
        assert not path.exists()

    with patch("subprocess.run", side_effect=run) as run_mock, patch("time.sleep"):
        run_command(label="example", module="example", arguments=["--dashboard-js", str(path)])
    assert run_mock.call_count == 2


def test_corrupt_cache_and_remote_archives_do_not_block_current_sources(tmp_path: Path, capsys) -> None:
    namespace = _script_namespace()
    build_site = namespace["build_site"]
    (tmp_path / "index.html").write_text("<html></html>")
    cache = tmp_path / ".refresh-cache"
    namespace["write_payload"]({"snapshots": [None]}, cache / "data/model_probability_history_data.js")
    namespace["write_payload"]({"meetings": [None]}, cache / "data/fed_rate_history_data.js")
    namespace["write_payload"]({"providers": None}, cache / "data/market_dashboard_data.js")
    remote = Mock(side_effect=TimeoutError("deployed archive unavailable"))
    ingested = []

    def ingest(**kwargs):
        ingested.append(kwargs["module"])
        _write_source_exports(namespace, kwargs["arguments"])

    with patch.dict(build_site.__globals__, {
        "REPO_ROOT": tmp_path, "run_ingestion_command": ingest,
        "fetch_existing_history": remote, "fetch_existing_backtest": remote,
        "fetch_existing_meetings": remote,
        "resolve_backtest_payload": Mock(return_value={"meetings": []}),
    }):
        with pytest.raises(RuntimeError, match="Deployment withheld"):
            build_site(output_dir=tmp_path / "_site", cache_dir=cache)
    assert len(ingested) == 11
    assert json.loads((cache / "data/model_probability_history_data.js").read_text().split(" = ", 1)[1].rstrip(";\n")) == {
        "snapshots": [None]
    }
    assert not (cache / "data/data_backtest_data.js").exists()
    assert namespace["read_payload"](cache / "data/fomc_history_data.js")["2026"][0]["decision"] == "raise"
    assert "Cached" in capsys.readouterr().out


def test_no_cache_uses_disposable_database_outside_artifact(tmp_path: Path) -> None:
    namespace = _script_namespace()
    build_site = namespace["build_site"]
    (tmp_path / "index.html").write_text("<html></html>")
    db_paths = []

    def ingest(**kwargs):
        arguments = kwargs["arguments"]
        if "--db-path" in arguments:
            db_path = Path(arguments[arguments.index("--db-path") + 1])
            db_paths.append(db_path)
            with sqlite3.connect(db_path) as connection:
                connection.execute("CREATE TABLE IF NOT EXISTS observations (value INTEGER)")
        _write_source_exports(namespace, arguments)

    with patch.dict(build_site.__globals__, {
        "REPO_ROOT": tmp_path, "run_ingestion_command": ingest,
        "resolve_backtest_payload": Mock(return_value={"meetings": []}),
    }):
        build_site(output_dir=tmp_path / "_site")
    assert len(set(db_paths)) == 1
    assert all(not path.exists() and tmp_path / "_site" not in path.parents for path in db_paths)


@pytest.mark.parametrize("cache_name", ["_site", "_site/cache"])
def test_cache_cannot_be_inside_site_artifact(tmp_path: Path, cache_name: str) -> None:
    build_site = _script_namespace()["build_site"]
    with patch.dict(build_site.__globals__, {"REPO_ROOT": tmp_path}):
        with pytest.raises(ValueError, match="outside the output"):
            build_site(output_dir=tmp_path / "_site", cache_dir=tmp_path / cache_name)


@pytest.mark.parametrize("protected_name", [".git", ".github", "assets", "config", "scripts", "tests"])
@pytest.mark.parametrize("argument", ["output_dir", "cache_dir"])
def test_build_cannot_replace_protected_repository_paths(
    tmp_path: Path, protected_name: str, argument: str
) -> None:
    build_site = _script_namespace()["build_site"]
    protected = tmp_path / protected_name
    protected.mkdir()
    sentinel = protected / "keep"
    sentinel.write_text("preserve")
    kwargs = {"output_dir": tmp_path / "_site", argument: protected}
    with patch.dict(build_site.__globals__, {"REPO_ROOT": tmp_path}):
        with pytest.raises(ValueError):
            build_site(**kwargs)
    assert sentinel.read_text() == "preserve"


def test_cached_observations_are_upserted_not_repeated(tmp_path: Path) -> None:
    namespace = _script_namespace()
    build_site = namespace["build_site"]
    (tmp_path / "index.html").write_text("<html></html>")
    cache = tmp_path / ".refresh-cache"
    value = 330.0

    def ingest(**kwargs):
        arguments = kwargs["arguments"]
        if kwargs["module"].endswith("cpi_ingestion"):
            store = CPIStore(Path(arguments[arguments.index("--db-path") + 1]))
            store.write_observations([
                CPIObservation("CUSR0000SA0", "2026-01-01", 2026, "M01", "January", value),
                CPIObservation("CUSR0000SA0", "2026-02-01", 2026, "M02", "February", 331.0),
            ])
        _write_source_exports(namespace, arguments)

    with patch.dict(build_site.__globals__, {
        "REPO_ROOT": tmp_path, "run_ingestion_command": ingest,
        "resolve_backtest_payload": Mock(return_value={"meetings": []}),
    }):
        build_site(output_dir=tmp_path / "_site", cache_dir=cache)
        value = 330.5
        build_site(output_dir=tmp_path / "_site", cache_dir=cache)
    with sqlite3.connect(cache / "market_snapshots.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM cpi_observations").fetchone()[0] == 2
        assert connection.execute(
            "SELECT value FROM cpi_observations WHERE observation_date = '2026-01-01'"
        ).fetchone()[0] == 330.5


def test_failed_events_refresh_also_prevents_model_snapshot(tmp_path: Path) -> None:
    namespace = _script_namespace()
    build_site = namespace["build_site"]
    (tmp_path / "index.html").write_text("<html></html>")
    cache = tmp_path / ".refresh-cache"
    history = {"snapshots": [{"captured_at": "2026-01-01", "target_meeting": "2026-09-16"}]}
    namespace["write_payload"](history, cache / "data/model_probability_history_data.js")
    snapshot = Mock(wraps=build_prediction_snapshot)

    def ingest(**kwargs):
        if kwargs["module"].endswith("fed_events_ingestion"):
            raise TimeoutError("events unavailable")
        _write_source_exports(namespace, kwargs["arguments"])

    with patch.dict(build_site.__globals__, {
        "REPO_ROOT": tmp_path, "run_ingestion_command": ingest,
        "build_prediction_snapshot": snapshot,
        "resolve_backtest_payload": Mock(return_value={"meetings": []}),
    }):
        build_site(output_dir=tmp_path / "_site", cache_dir=cache)
    snapshot.assert_not_called()
    assert namespace["read_payload"](cache / "data/model_probability_history_data.js") == history
    assert not list((tmp_path / "_site").rglob("*.db"))
    assert not (tmp_path / "_site/.refresh-cache").exists()
    status = namespace["read_payload"](tmp_path / "_site/data/refresh_status_data.js")
    assert status["failures"] == ["Federal Reserve events", "Model snapshot"]


def test_complete_refresh_retains_saved_snapshots_without_relying_on_network(tmp_path: Path) -> None:
    namespace = _script_namespace()
    build_site = namespace["build_site"]
    (tmp_path / "index.html").write_text("<html></html>")
    cache = tmp_path / ".refresh-cache"
    saved = {"snapshots": [
        {"captured_at": "2026-01-01", "target_meeting": "2026-03-18", "decision": "hold"},
        {"captured_at": "2026-04-01", "target_meeting": "2026-06-17", "decision": "cut"},
    ]}
    namespace["write_payload"](saved, cache / "data/model_probability_history_data.js")
    remote_history = Mock(side_effect=TimeoutError("probability history timed out"))
    ingested = []

    def ingest(**kwargs):
        ingested.append(kwargs["module"])
        _write_source_exports(namespace, kwargs["arguments"])

    def fred(**kwargs):
        assert len(ingested) == 11
        raise TimeoutError("FRED timed out")

    with patch.dict(build_site.__globals__, {
        "REPO_ROOT": tmp_path, "run_ingestion_command": ingest,
        "fetch_existing_history": remote_history,
        "fetch_history_payload": fred,
        "resolve_backtest_payload": Mock(return_value={"meetings": []}),
    }):
        build_site(
            output_dir=tmp_path / "_site", cache_dir=cache,
            history_source_url="https://example.test/history.js", refresh_fed_history=True,
        )
    remote_history.assert_not_called()
    for directory in (cache, tmp_path / "_site"):
        history = namespace["read_payload"](directory / "data/model_probability_history_data.js")
        assert history["snapshots"][:2] == saved["snapshots"]
        assert len(history["snapshots"]) == 3


@pytest.mark.parametrize("archive_name", [
    "model_probability_history_data.js", "data_backtest_data.js", "fed_rate_history_data.js",
])
@pytest.mark.parametrize("failure_kind", ["http", "network"])
def test_unknown_deployed_archive_is_not_replaced_and_next_run_can_recover(
    tmp_path: Path, archive_name: str, failure_kind: str
) -> None:
    namespace = _script_namespace()
    build_site = namespace["build_site"]
    (tmp_path / "index.html").write_text("<html></html>")
    cache, output = tmp_path / ".refresh-cache", tmp_path / "_site"
    source_url = f"https://example.test/{archive_name}"
    error = (
        HTTPError(source_url, 500, "unavailable", {}, None)
        if failure_kind == "http" else URLError("connection failed")
    )
    option = {
        "model_probability_history_data.js": "history_source_url",
        "data_backtest_data.js": "backtest_source_url",
        "fed_rate_history_data.js": "fed_history_source_url",
    }[archive_name]
    ingested = []

    def ingest(**kwargs):
        ingested.append(kwargs["module"])
        arguments = kwargs["arguments"]
        if "--db-path" in arguments:
            with sqlite3.connect(arguments[arguments.index("--db-path") + 1]) as connection:
                connection.execute("CREATE TABLE IF NOT EXISTS saved_sources (source TEXT PRIMARY KEY)")
                connection.execute("INSERT OR REPLACE INTO saved_sources VALUES (?)", (kwargs["module"],))
        _write_source_exports(namespace, arguments)

    archived = (
        {"snapshots": [{"captured_at": "2026-01-01", "target_meeting": "2026-03-18", "decision": "hold"}]}
        if archive_name == "model_probability_history_data.js"
        else {"meetings": [{
            "meeting_date": "2026-01-01", "decision": "hold", "target_lower": 3.5, "target_upper": 3.75,
        }]}
    )
    response = MagicMock()
    response.__enter__.return_value.read.return_value = (
        f"window.{namespace['PAYLOAD_NAMES'][archive_name]} = {json.dumps(archived)};"
    ).encode()
    network = Mock(side_effect=error)
    with patch.dict(build_site.__globals__, {
        "REPO_ROOT": tmp_path, "run_ingestion_command": ingest, "urlopen": network,
        "resolve_backtest_payload": Mock(return_value={"meetings": []}),
    }):
        with pytest.raises(RuntimeError, match="Deployment withheld"):
            build_site(output_dir=output, cache_dir=cache, **{option: source_url})
        assert len(ingested) == 11
        assert not (cache / "data" / archive_name).exists()
        assert not (output / "data" / archive_name).exists()
        assert not (output / "index.html").exists()
        assert namespace["read_payload"](cache / "data/cpi_dashboard_data.js")["metrics"]["cpi_bias"] == 0.2
        with sqlite3.connect(cache / "market_snapshots.db") as connection:
            assert connection.execute("SELECT COUNT(*) FROM saved_sources").fetchone()[0] == 10
        network.side_effect = None
        network.return_value = response
        build_site(output_dir=output, cache_dir=cache, **{option: source_url})
    restored = namespace["read_payload"](cache / "data" / archive_name)
    if "snapshots" in archived:
        assert archived["snapshots"][0] in restored["snapshots"]
        assert len(restored["snapshots"]) == 2
    else:
        assert restored == archived
    assert (output / "index.html").is_file()
    assert network.call_count == 2


def test_confirmed_history_404_allows_new_archive(tmp_path: Path) -> None:
    namespace = _script_namespace()
    build_site = namespace["build_site"]
    (tmp_path / "index.html").write_text("<html></html>")
    source_url = "https://example.test/history.js"
    with patch.dict(build_site.__globals__, {
        "REPO_ROOT": tmp_path,
        "run_ingestion_command": lambda **kwargs: _write_source_exports(namespace, kwargs["arguments"]),
        "urlopen": Mock(side_effect=HTTPError(source_url, 404, "missing", {}, None)),
    }):
        build_site(
            output_dir=tmp_path / "_site", cache_dir=tmp_path / ".refresh-cache",
            history_source_url=source_url,
        )
    assert len(namespace["read_payload"](
        tmp_path / ".refresh-cache/data/model_probability_history_data.js"
    )["snapshots"]) == 1


@pytest.mark.parametrize("unknown_model", [False, True])
def test_complete_fred_refresh_recovers_rate_archive_but_not_unknown_model_history(
    tmp_path: Path, unknown_model: bool
) -> None:
    namespace = _script_namespace()
    build_site = namespace["build_site"]
    (tmp_path / "index.html").write_text("<html></html>")
    cache = tmp_path / ".refresh-cache"
    ingested = []
    archive = {"meetings": [{
        "meeting_date": "1982-01-01", "decision": "hold", "target_lower": 12.0, "target_upper": 12.0,
    }]}

    def ingest(**kwargs):
        ingested.append(kwargs["module"])
        _write_source_exports(namespace, kwargs["arguments"])

    def fred(**kwargs):
        assert len(ingested) == 11
        assert (cache / "data/cpi_dashboard_data.js").is_file()
        return archive

    option = "history_source_url" if unknown_model else "fed_history_source_url"
    with patch.dict(build_site.__globals__, {
        "REPO_ROOT": tmp_path, "run_ingestion_command": ingest,
        "urlopen": Mock(side_effect=TimeoutError("archive timed out")),
        "fetch_history_payload": fred,
        "resolve_backtest_payload": Mock(return_value={"meetings": []}),
    }):
        kwargs = {
            "output_dir": tmp_path / "_site", "cache_dir": cache, "refresh_fed_history": True,
            option: "https://example.test/history.js",
        }
        if unknown_model:
            with pytest.raises(RuntimeError, match="model_probability_history_data.js"):
                build_site(**kwargs)
            assert not (cache / "data/model_probability_history_data.js").exists()
            assert not (tmp_path / "_site/index.html").exists()
        else:
            build_site(**kwargs)
            assert (tmp_path / "_site/index.html").exists()
    assert namespace["read_payload"](cache / "data/fed_rate_history_data.js")["meetings"] == archive["meetings"]


def test_prediction_snapshot_records_all_three_probabilities() -> None:
    snapshot = build_prediction_snapshot(
        _complete_payloads(),
        captured_at="2026-08-30T12:00:00+00:00",
    )

    assert snapshot["target_meeting"] == "2026-09-16"
    assert snapshot["decision"] == "hold"
    assert snapshot["model_version"] == "data-v1"
    assert sum(snapshot["probabilities"].values()) == pytest.approx(1.0, abs=0.000002)
    assert snapshot["confidence"] == snapshot["probabilities"]["hold"]
    assert snapshot["market_prediction"]["decision"] == "raise"
    assert snapshot["market_prediction"]["probabilities"] == {
        "raise": 0.55,
        "hold": 0.4,
        "cut": 0.05,
    }


def test_recent_hike_favors_wait_and_see_without_adding_raise_pressure() -> None:
    after_hike = build_prediction_snapshot(_complete_payloads())
    after_hold_payloads = deepcopy(_complete_payloads())
    after_hold_payloads["fomc"]["decision"] = "hold"
    after_hold = build_prediction_snapshot(after_hold_payloads)

    assert after_hike["probabilities"]["hold"] > after_hold["probabilities"]["hold"]
    assert after_hike["probabilities"]["cut"] < after_hold["probabilities"]["cut"]
    assert after_hike["probabilities"]["raise"] < after_hold["probabilities"]["raise"]


def test_prediction_snapshot_rejects_completed_target_meeting() -> None:
    payloads = _complete_payloads()
    payloads["fomc"]["meeting_date"] = payloads["market"]["target_meeting"]

    with pytest.raises(PredictionHistoryError, match="later than"):
        build_prediction_snapshot(payloads)


def test_prediction_snapshot_requires_both_market_providers() -> None:
    payloads = _complete_payloads()
    del payloads["market"]["providers"]["kalshi"]

    with pytest.raises(PredictionHistoryError, match="kalshi"):
        build_prediction_snapshot(payloads)


def test_merge_history_preserves_meetings_and_replaces_duplicate_snapshot() -> None:
    old = {
        "snapshots": [
            {
                "captured_at": "2026-08-29T12:00:00+00:00",
                "target_meeting": "2026-09-16",
                "decision": "raise",
            },
            {
                "captured_at": "2026-08-30T12:00:00+00:00",
                "target_meeting": "2026-09-16",
                "decision": "raise",
            },
        ]
    }
    replacement = {
        "captured_at": "2026-08-30T12:00:00+00:00",
        "target_meeting": "2026-09-16",
        "decision": "hold",
    }

    result = merge_history(old, replacement, generated_at="2026-08-30T12:01:00+00:00")

    assert len(result["snapshots"]) == 2
    assert result["snapshots"][-1]["decision"] == "hold"
