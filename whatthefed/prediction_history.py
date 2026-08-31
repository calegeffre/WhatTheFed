"""Build and persist snapshots of the all-data decision model."""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Mapping


MACRO_WEIGHTS = {
    "cpi": 0.24,
    "ppi": 0.12,
    "labor": 0.21,
    "gdp": 0.12,
    "breakeven": 0.14,
    "treasury": 0.08,
    "policyRate": 0.05,
    "fiscal": 0.04,
}
ENSEMBLE_WEIGHTS = {"market": 0.45, "policy": 0.55}
MODEL_VERSION = "all-data-v1"
PAYLOAD_GLOBALS = {
    "fomc": ("fomc_dashboard_data.js", "__FOMC_DASHBOARD_DATA__"),
    "market": ("market_dashboard_data.js", "__MARKET_DASHBOARD_DATA__"),
    "cpi": ("cpi_dashboard_data.js", "__CPI_DASHBOARD_DATA__"),
    "labor": ("labor_dashboard_data.js", "__LABOR_DASHBOARD_DATA__"),
    "treasury": ("treasury_dashboard_data.js", "__TREASURY_DASHBOARD_DATA__"),
    "policy_rate": ("policy_rate_dashboard_data.js", "__POLICY_RATE_DASHBOARD_DATA__"),
    "breakeven": ("breakeven_dashboard_data.js", "__BREAKEVEN_DASHBOARD_DATA__"),
    "ppi": ("ppi_dashboard_data.js", "__PPI_DASHBOARD_DATA__"),
    "fiscal": ("fiscal_dashboard_data.js", "__FISCAL_DASHBOARD_DATA__"),
    "gdp": ("gdp_dashboard_data.js", "__GDP_DASHBOARD_DATA__"),
}


class PredictionHistoryError(RuntimeError):
    """Raised when a complete model snapshot cannot be produced."""


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _number(value: object) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise PredictionHistoryError(f"Expected a numeric model input, got {value!r}.") from error
    if not math.isfinite(parsed):
        raise PredictionHistoryError(f"Expected a finite model input, got {value!r}.")
    return parsed


def _normalize_triplet(values: object) -> dict[str, float]:
    if not isinstance(values, Mapping):
        raise PredictionHistoryError("Prediction-market probabilities are missing.")
    parsed = {key: _number(values.get(key, 0.0)) for key in ("raise", "hold", "cut")}
    total = sum(parsed.values())
    if total <= 0:
        raise PredictionHistoryError("Prediction-market probabilities sum to zero.")
    return {key: value / total for key, value in parsed.items()}


def _payload_metric(payload: object, key: str) -> float:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("metrics"), Mapping):
        raise PredictionHistoryError(f"Payload containing {key} is missing.")
    return _number(payload["metrics"].get(key))


def _parse_date(value: object, label: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as error:
        raise PredictionHistoryError(f"{label} is missing or invalid.") from error


def _probabilities_from_bias(bias: float, confidence: float) -> dict[str, float]:
    temperature = 1.6 + 2.4 * _clamp(confidence, 0.0, 1.0)
    utilities = {"raise": bias, "hold": 0.45, "cut": -bias}
    weights = {key: math.exp(value * temperature) for key, value in utilities.items()}
    total = sum(weights.values())
    return {key: value / total for key, value in weights.items()}


def build_prediction_snapshot(
    payloads: Mapping[str, object],
    *,
    captured_at: str | None = None,
) -> dict[str, object]:
    """Calculate one snapshot using the same formula as the browser headline."""

    market = payloads.get("market")
    fomc = payloads.get("fomc")
    treasury = payloads.get("treasury")
    if not isinstance(market, Mapping) or not isinstance(fomc, Mapping):
        raise PredictionHistoryError("FOMC or prediction-market payload is missing.")

    providers = market.get("providers")
    if not isinstance(providers, Mapping):
        raise PredictionHistoryError("Prediction-market providers are missing.")
    for provider in ("kalshi", "polymarket"):
        provider_payload = providers.get(provider)
        if not isinstance(provider_payload, Mapping):
            raise PredictionHistoryError(f"{provider} market data is missing.")
        _normalize_triplet(provider_payload.get("probabilities"))

    market_probabilities = _normalize_triplet(market.get("blended_probabilities"))
    signals = fomc.get("signals")
    if not isinstance(signals, list):
        raise PredictionHistoryError("FOMC signals are missing.")
    prior_signal = next(
        (
            signal
            for signal in signals
            if isinstance(signal, Mapping) and "bias" in str(signal.get("label", "")).lower()
        ),
        None,
    )
    if prior_signal is None:
        raise PredictionHistoryError("FOMC bias is missing.")
    previous_bias = _number(prior_signal.get("display"))

    if not isinstance(treasury, Mapping) or not isinstance(treasury.get("points"), list):
        raise PredictionHistoryError("Treasury curve data is missing.")
    yields = {
        str(point.get("maturity")): _number(point.get("yield_pct"))
        for point in treasury["points"]
        if isinstance(point, Mapping) and str(point.get("maturity")) in {"2Y", "10Y"}
    }
    if set(yields) != {"2Y", "10Y"}:
        raise PredictionHistoryError("Treasury 2Y or 10Y yield is missing.")

    macro_inputs = {
        "cpi": _payload_metric(payloads.get("cpi"), "cpi_bias"),
        "ppi": _payload_metric(payloads.get("ppi"), "ppi_bias"),
        "labor": _payload_metric(payloads.get("labor"), "labor_bias"),
        "gdp": _payload_metric(payloads.get("gdp"), "gdp_bias"),
        "breakeven": _payload_metric(payloads.get("breakeven"), "breakeven_bias"),
        "treasury": (yields["10Y"] - yields["2Y"]) / 1.5,
        "policyRate": _payload_metric(payloads.get("policy_rate"), "policy_rate_bias"),
        "fiscal": _payload_metric(payloads.get("fiscal"), "fiscal_bias"),
    }
    macro_bias = _clamp(
        sum(MACRO_WEIGHTS[key] * _clamp(value, -1.0, 1.0) for key, value in macro_inputs.items()),
        -1.0,
        1.0,
    )

    meeting_date = _parse_date(market.get("target_meeting"), "Target meeting")
    previous_meeting_date = _parse_date(fomc.get("meeting_date"), "Previous FOMC meeting")
    days_to_meeting = abs((meeting_date - previous_meeting_date).days)
    expected_monthly_releases = max(1, math.floor(days_to_meeting / 30 + 0.5))
    carry = _clamp(1 / (1 + expected_monthly_releases * 0.55), 0.15, 0.9)
    policy_bias = _clamp(previous_bias * carry + macro_bias * (1 - carry), -1.0, 1.0)
    market_bias = market_probabilities["raise"] - market_probabilities["cut"]
    bias = _clamp(
        ENSEMBLE_WEIGHTS["market"] * market_bias + ENSEMBLE_WEIGHTS["policy"] * policy_bias,
        -1.0,
        1.0,
    )
    probabilities = _probabilities_from_bias(bias, max(market_probabilities.values()))
    decision = max(probabilities, key=probabilities.__getitem__)

    snapshot_time = captured_at or datetime.now(timezone.utc).isoformat()
    return {
        "captured_at": snapshot_time,
        "target_meeting": meeting_date.isoformat(),
        "model_version": MODEL_VERSION,
        "decision": decision,
        "confidence": round(probabilities[decision], 6),
        "bias": round(bias, 6),
        "probabilities": {key: round(value, 6) for key, value in probabilities.items()},
    }


def read_js_payload(path: Path, global_name: str) -> object:
    text = path.read_text(encoding="utf-8").strip()
    prefix = f"window.{global_name} = "
    if not text.startswith(prefix) or not text.endswith(";"):
        raise PredictionHistoryError(f"{path.name} does not contain {global_name}.")
    return json.loads(text[len(prefix) : -1])


def load_dashboard_payloads(data_dir: Path) -> dict[str, object]:
    return {
        key: read_js_payload(data_dir / filename, global_name)
        for key, (filename, global_name) in PAYLOAD_GLOBALS.items()
    }


def merge_history(
    existing_history: object,
    snapshot: Mapping[str, object],
    *,
    generated_at: str | None = None,
) -> dict[str, object]:
    snapshots: list[dict[str, object]] = []
    if isinstance(existing_history, Mapping) and isinstance(existing_history.get("snapshots"), list):
        snapshots = [
            dict(item)
            for item in existing_history["snapshots"]
            if isinstance(item, Mapping)
        ]

    identity = (snapshot.get("target_meeting"), snapshot.get("captured_at"))
    snapshots = [
        item
        for item in snapshots
        if (item.get("target_meeting"), item.get("captured_at")) != identity
    ]
    snapshots.append(dict(snapshot))
    snapshots.sort(key=lambda item: (str(item.get("target_meeting", "")), str(item.get("captured_at", ""))))
    return {
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "snapshots": snapshots,
    }


def export_history_js(history: Mapping[str, object], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        "window.__MODEL_PROBABILITY_HISTORY_DATA__ = "
        + json.dumps(history, sort_keys=True, indent=2)
        + ";\n",
        encoding="utf-8",
    )
