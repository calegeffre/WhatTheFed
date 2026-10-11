from __future__ import annotations

from copy import deepcopy
import json
import runpy
import subprocess
from urllib.error import HTTPError
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, Mock, call, patch

import pytest

from whatthefed.prediction_history import (
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
    fresh_history = {"meetings": [{"meeting_date": "2026-09-16"}]}
    fetch_fred = Mock(return_value=fresh_history)
    fetch_cached = Mock(return_value=cached_history)
    resolver = Mock(return_value=cached_backtest or {"meetings": []})

    def ingest(*, label: str, module: str, arguments: list[str]) -> None:
        for index, argument in enumerate(arguments):
            if argument in {"--dashboard-js", "--history-js", "--kg-js"}:
                Path(arguments[index + 1]).write_text("window.__TEST_DATA__ = {};")

    with patch.dict(build_site.__globals__, {
        "REPO_ROOT": tmp_path,
        "fetch_existing_history": Mock(return_value={"snapshots": []}),
        "fetch_existing_backtest": Mock(return_value=cached_backtest),
        "fetch_existing_meetings": fetch_cached,
        "fetch_history_payload": fetch_fred,
        "run_ingestion_command": ingest,
        "resolve_backtest_payload": resolver,
        "load_dashboard_payloads": Mock(return_value={}),
        "build_prediction_snapshot": Mock(return_value={"captured_at": "2026-09-01"}),
    }):
        build_site(
            output_dir=output_dir,
            fed_history_source_url="https://example.test/history.js",
            refresh_fed_history=refresh,
        )

    if refresh:
        fetch_fred.assert_called_once_with(start_year=1982)
        fetch_cached.assert_not_called()
    else:
        fetch_fred.assert_not_called()
        fetch_cached.assert_called_once()
    if cached_backtest is not None or (cached_history and cached_history["meetings"]) or refresh:
        resolver.assert_called_once()
    else:
        resolver.assert_not_called()
    text = (output_dir / "data" / "fed_rate_history_data.js").read_text()
    assert json.loads(text.split(" = ", 1)[1].rstrip(";\n")) == (
        fresh_history if refresh else cached_history or {"meetings": []}
    )
    assert all((output_dir / "data" / name).is_file() for name in namespace["EXPECTED_PAYLOADS"])
    assert (output_dir / "index.html").is_file()
    assert not list(output_dir.rglob("*.db"))


def test_explicit_fed_history_refresh_failure_stops_build(tmp_path: Path) -> None:
    build_site = _script_namespace()["build_site"]
    with patch.dict(build_site.__globals__, {
        "REPO_ROOT": tmp_path,
        "fetch_history_payload": Mock(side_effect=TimeoutError("FRED timed out")),
    }):
        with pytest.raises(TimeoutError, match="FRED timed out"):
            build_site(output_dir=tmp_path / "_site", refresh_fed_history=True)
    assert not (tmp_path / "_site").exists()


def test_cli_passes_fed_history_options() -> None:
    main = _script_namespace()["main"]
    build_site = Mock()
    with patch.dict(main.__globals__, {"build_site": build_site}):
        assert main([
            "--fed-history-source-url", "https://example.test/history.js",
            "--refresh-fed-history",
        ]) == 0
    assert build_site.call_args.kwargs["refresh_fed_history"] is True
    assert build_site.call_args.kwargs["fed_history_source_url"] == "https://example.test/history.js"


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
