"""Walk-forward evaluation of the macro-data FOMC decision model."""

from __future__ import annotations

import argparse
import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

from .prediction_history import MACRO_WEIGHTS, calculate_data_prediction, read_js_payload


HISTORY_GLOBALS = {
    "cpi": ("cpi_dashboard_data.js", "__CPI_DASHBOARD_DATA__", "bias_history", 20),
    "ppi": ("ppi_dashboard_data.js", "__PPI_DASHBOARD_DATA__", "bias_history", 20),
    "labor": ("labor_dashboard_data.js", "__LABOR_DASHBOARD_DATA__", "bias_history", 20),
    "gdp": ("gdp_dashboard_data.js", "__GDP_DASHBOARD_DATA__", "bias_history", 35),
    "breakeven": ("breakeven_dashboard_data.js", "__BREAKEVEN_DASHBOARD_DATA__", "bias_history", 0),
    "treasury": ("treasury_dashboard_data.js", "__TREASURY_DASHBOARD_DATA__", "slope_history", 0),
    "policyRate": ("policy_rate_dashboard_data.js", "__POLICY_RATE_DASHBOARD_DATA__", "bias_history", 0),
    "fiscal": ("fiscal_dashboard_data.js", "__FISCAL_DASHBOARD_DATA__", "bias_history", 35),
}
MONTHLY_PERIOD_DOMAINS = {"cpi", "ppi", "labor"}


class BacktestError(RuntimeError):
    """Raised when a valid walk-forward backtest cannot be produced."""


def build_backtest_payload(
    *,
    meetings: list[Mapping[str, object]],
    histories: Mapping[str, list[Mapping[str, object]]],
    years: int = 5,
    as_of: date | None = None,
    generated_at: str | None = None,
) -> dict[str, object]:
    completed = sorted(
        (
            item
            for item in meetings
            if str(item.get("decision")) in {"raise", "hold", "cut"}
            and _date_or_none(item.get("meeting_date")) is not None
        ),
        key=lambda item: str(item["meeting_date"]),
    )
    if not completed:
        raise BacktestError("No completed FOMC meetings are available for backtesting.")
    end_date = min(as_of or date.today(), _date_or_none(completed[-1]["meeting_date"]) or date.today())
    start_date = _shift_years(end_date, -years)
    results: list[dict[str, object]] = []

    for index, meeting in enumerate(completed):
        meeting_date = _date_or_none(meeting["meeting_date"])
        if meeting_date is None or meeting_date < start_date or meeting_date > end_date or index == 0:
            continue
        previous_decision = str(completed[index - 1]["decision"])
        macro_inputs: dict[str, float] = {}
        input_dates: dict[str, str] = {}
        for key, history in histories.items():
            observation = _latest_available(
                history,
                meeting_date,
                HISTORY_GLOBALS[key][3],
                monthly_period=key in MONTHLY_PERIOD_DOMAINS,
            )
            if observation is None:
                break
            macro_inputs[key] = float(observation["bias"])
            input_dates[key] = str(observation["date"])
        if set(macro_inputs) != set(MACRO_WEIGHTS):
            continue

        prediction = calculate_data_prediction(macro_inputs, previous_decision)
        actual = str(meeting["decision"])
        probabilities = prediction["probabilities"]
        brier = sum((float(probabilities[key]) - (1.0 if key == actual else 0.0)) ** 2 for key in probabilities)
        log_loss = -math.log(max(float(probabilities[actual]), 1e-12))
        results.append(
            {
                "meeting_date": meeting_date.isoformat(),
                "actual": actual,
                "predicted": prediction["decision"],
                "confidence": round(float(prediction["confidence"]), 6),
                "probabilities": {key: round(float(value), 6) for key, value in probabilities.items()},
                "macro_bias": round(float(prediction["macro_bias"]), 6),
                "correct": prediction["decision"] == actual,
                "brier_score": round(brier, 6),
                "log_loss": round(log_loss, 6),
                "input_dates": input_dates,
            }
        )

    if not results:
        raise BacktestError("No meetings had complete lagged macro histories for the requested window.")
    correct = sum(bool(item["correct"]) for item in results)
    outcome_counts = {
        key: sum(str(item["actual"]) == key for item in results)
        for key in ("raise", "hold", "cut")
    }
    return {
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "model_version": "data-v1",
        "window_years": years,
        "coverage_start": results[0]["meeting_date"],
        "coverage_end": results[-1]["meeting_date"],
        "meeting_count": len(results),
        "accuracy": round(correct / len(results), 6),
        "mean_brier_score": round(sum(float(item["brier_score"]) for item in results) / len(results), 6),
        "mean_log_loss": round(sum(float(item["log_loss"]) for item in results) / len(results), 6),
        "outcome_counts": outcome_counts,
        "limitations": [
            "Inputs use observations dated before each meeting with conservative publication lags.",
            "Historical values may include later revisions because true vintage releases are not available for every domain.",
            "This is an evaluation of directional meeting decisions, not target-rate magnitude.",
        ],
        "meetings": results,
    }


def load_backtest_inputs(data_dir: Path) -> tuple[list[Mapping[str, object]], dict[str, list[Mapping[str, object]]]]:
    fed_history = read_js_payload(data_dir / "fed_rate_history_data.js", "__FED_RATE_HISTORY_DATA__")
    if not isinstance(fed_history, Mapping) or not isinstance(fed_history.get("meetings"), list):
        raise BacktestError("Fed rate history payload is missing meetings.")
    histories: dict[str, list[Mapping[str, object]]] = {}
    for key, (filename, global_name, history_key, _) in HISTORY_GLOBALS.items():
        payload = read_js_payload(data_dir / filename, global_name)
        history = payload.get(history_key) if isinstance(payload, Mapping) else None
        if not isinstance(history, list):
            raise BacktestError(f"{key} history is missing.")
        histories[key] = [item for item in history if isinstance(item, Mapping)]
    return fed_history["meetings"], histories


def export_backtest_js(payload: Mapping[str, object], output_path: str | Path) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "window.__DATA_BACKTEST_DATA__ = " + json.dumps(payload, sort_keys=True, indent=2) + ";\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest the macro-data FOMC model.")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--output-js", required=True)
    args = parser.parse_args(argv)
    meetings, histories = load_backtest_inputs(args.data_dir)
    payload = build_backtest_payload(meetings=meetings, histories=histories, years=args.years)
    export_backtest_js(payload, args.output_js)
    print(f"Backtested {payload['meeting_count']} meetings ({payload['coverage_start']} to {payload['coverage_end']}).")
    return 0


def _latest_available(
    history: list[Mapping[str, object]],
    meeting_date: date,
    lag_days: int,
    *,
    monthly_period: bool = False,
) -> Mapping[str, object] | None:
    eligible: list[tuple[date, Mapping[str, object]]] = []
    for item in history:
        observation_date = _period_end_or_none(item.get("date"), monthly_period=monthly_period)
        try:
            bias = float(item.get("bias"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(bias) or observation_date is None:
            continue
        if observation_date + timedelta(days=lag_days) < meeting_date:
            eligible.append((observation_date, item))
    return max(eligible, key=lambda pair: pair[0])[1] if eligible else None


def _period_end_or_none(value: object, *, monthly_period: bool = False) -> date | None:
    text = str(value or "")
    if len(text) == 6 and text[4] == "Q" and text[5] in "1234":
        quarter = int(text[5])
        month = quarter * 3
        next_month = date(int(text[:4]) + (month == 12), 1 if month == 12 else month + 1, 1)
        return next_month - timedelta(days=1)
    parsed = _date_or_none(text)
    if parsed is None or not monthly_period:
        return parsed
    next_month = date(
        parsed.year + (parsed.month == 12),
        1 if parsed.month == 12 else parsed.month + 1,
        1,
    )
    return next_month - timedelta(days=1)


def _date_or_none(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _shift_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        return value.replace(year=value.year + years, day=28)


if __name__ == "__main__":
    raise SystemExit(main())
