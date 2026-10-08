from datetime import date

import pytest

from whatthefed.backtest import _latest_available, build_backtest_payload
from whatthefed.prediction_history import MACRO_WEIGHTS


def test_monthly_observation_is_unavailable_until_period_end_plus_lag() -> None:
    history = [
        {"date": "2024-01-01", "bias": 0.1},
        {"date": "2024-02-01", "bias": 0.2},
    ]

    assert _latest_available(
        history,
        date(2024, 2, 15),
        20,
        monthly_period=True,
    ) is None
    assert _latest_available(
        history,
        date(2024, 2, 21),
        20,
        monthly_period=True,
    ) == history[0]


def test_backtest_uses_only_complete_lagged_inputs_and_calculates_metrics() -> None:
    meetings = [
        {"meeting_date": "2023-12-13", "decision": "hold"},
        {"meeting_date": "2024-03-20", "decision": "hold"},
        {"meeting_date": "2024-06-12", "decision": "raise"},
    ]
    histories = {
        key: [
            {"date": "2023-10-01" if key in {"cpi", "ppi", "labor"} else "2023-10-31", "bias": 0.0},
            {"date": "2024-04-01" if key in {"cpi", "ppi", "labor"} else "2024-04-30", "bias": 0.5},
        ]
        for key in MACRO_WEIGHTS
    }

    payload = build_backtest_payload(
        meetings=meetings,
        histories=histories,
        years=5,
        as_of=date(2024, 6, 12),
        generated_at="2024-06-13T00:00:00Z",
    )

    assert payload["meeting_count"] == 2
    assert payload["coverage_start"] == "2024-03-20"
    assert payload["coverage_end"] == "2024-06-12"
    assert 0 <= payload["accuracy"] <= 1
    assert payload["mean_brier_score"] >= 0
    assert payload["mean_log_loss"] >= 0
    assert sum(payload["outcome_counts"].values()) == 2
    assert sum(payload["meetings"][0]["probabilities"].values()) == pytest.approx(1.0)
