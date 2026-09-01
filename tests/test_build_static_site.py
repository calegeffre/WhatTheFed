from __future__ import annotations

import runpy
import subprocess
from pathlib import Path
from unittest.mock import call, patch

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
    assert snapshot["decision"] == "raise"
    assert snapshot["model_version"] == "all-data-v2"
    assert sum(snapshot["probabilities"].values()) == pytest.approx(1.0, abs=0.000002)
    assert snapshot["confidence"] == snapshot["probabilities"]["raise"]


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
