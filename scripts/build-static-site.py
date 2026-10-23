"""Build the static dashboard, optionally retaining successful ingestions locally."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from whatthefed.prediction_history import (
    PAYLOAD_GLOBALS,
    build_prediction_snapshot,
    load_dashboard_payloads,
    merge_history,
)
from whatthefed.backtest import build_backtest_payload, load_backtest_inputs
from whatthefed.fed_history import fetch_history_payload
from whatthefed.meeting_evidence import build_neo4j_evidence, export_evidence_js
from whatthefed.neo4j_connection import load_settings

EXPECTED_PAYLOADS = (
    "fomc_dashboard_data.js",
    "fomc_history_data.js",
    "fed_rate_history_data.js",
    "data_backtest_data.js",
    "meeting_evidence_data.js",
    "market_dashboard_data.js",
    "cpi_dashboard_data.js",
    "kg_dashboard_data.js",
    "labor_dashboard_data.js",
    "labor_kg_dashboard_data.js",
    "treasury_dashboard_data.js",
    "policy_rate_dashboard_data.js",
    "breakeven_dashboard_data.js",
    "ppi_dashboard_data.js",
    "fiscal_dashboard_data.js",
    "gdp_dashboard_data.js",
    "fed_events_dashboard_data.js",
    "model_probability_history_data.js",
    "refresh_status_data.js",
)
MAX_COMMAND_ATTEMPTS = 3
MAX_COMMAND_SECONDS = 180
RETRY_DELAYS_SECONDS = (10, 30)
PAYLOAD_NAMES = {filename: global_name for filename, global_name in PAYLOAD_GLOBALS.values()}
PAYLOAD_NAMES.update({
    "fomc_history_data.js": "__FOMC_HISTORY_DATA__",
    "kg_dashboard_data.js": "__KG_DASHBOARD_DATA__",
    "labor_kg_dashboard_data.js": "__LABOR_KG_DASHBOARD_DATA__",
    "fed_events_dashboard_data.js": "__FED_EVENTS_DASHBOARD_DATA__",
    "fed_rate_history_data.js": "__FED_RATE_HISTORY_DATA__",
    "data_backtest_data.js": "__DATA_BACKTEST_DATA__",
    "meeting_evidence_data.js": "__MEETING_EVIDENCE_DATA__",
    "model_probability_history_data.js": "__MODEL_PROBABILITY_HISTORY_DATA__",
    "refresh_status_data.js": "__REFRESH_STATUS_DATA__",
})
EXPORT_OPTIONS = {"--dashboard-js", "--history-js", "--kg-js"}


def warn(message: str) -> None:
    print(f"::warning::{message}", flush=True)


def reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON value: {value}")


def validate_archive(name: str, payload: object) -> None:
    list_key = "snapshots" if name == "model_probability_history_data.js" else "meetings"
    if not isinstance(payload, dict) or not isinstance(payload.get(list_key), list):
        raise ValueError(f"{name} is missing {list_key}.")
    for item in payload[list_key]:
        if not isinstance(item, dict):
            raise ValueError(f"{name} contains invalid archive entries.")
        if list_key == "meetings":
            date.fromisoformat(str(item.get("meeting_date")))
        elif not isinstance(item.get("captured_at"), str) or not isinstance(item.get("target_meeting"), str):
            raise ValueError(f"{name} contains incomplete snapshots.")


def read_payload(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8").strip()
    prefix = f"window.{PAYLOAD_NAMES[path.name]} = "
    if not text.startswith(prefix) or not text.endswith(";"):
        raise ValueError(f"{path.name} has an invalid JavaScript assignment.")
    payload = json.loads(text[len(prefix):-1], parse_constant=reject_json_constant)
    if not isinstance(payload, dict) or not payload:
        raise ValueError(f"{path.name} has an empty or invalid payload.")
    list_key = {
        "fed_rate_history_data.js": "meetings",
        "data_backtest_data.js": "meetings",
        "model_probability_history_data.js": "snapshots",
        "fed_events_dashboard_data.js": "events",
        "refresh_status_data.js": "failures",
    }.get(path.name)
    if list_key and not isinstance(payload.get(list_key), list):
        raise ValueError(f"{path.name} is missing {list_key}.")
    if list_key in {"meetings", "snapshots"}:
        validate_archive(path.name, payload)
    if path.name == "fomc_history_data.js" and not all(
        isinstance(items, list) and all(
            isinstance(item, dict) and isinstance(item.get("meeting_date"), str) for item in items
        )
        for items in payload.values()
    ):
        raise ValueError("FOMC history has invalid meeting groups.")
    if path.name == "fomc_history_data.js":
        for group in payload.values():
            for item in group:
                date.fromisoformat(item["meeting_date"])
    metric_names = {
        "cpi_dashboard_data.js": "cpi_bias",
        "labor_dashboard_data.js": "labor_bias",
        "ppi_dashboard_data.js": "ppi_bias",
        "gdp_dashboard_data.js": "gdp_bias",
        "breakeven_dashboard_data.js": "breakeven_bias",
        "policy_rate_dashboard_data.js": "policy_rate_bias",
        "fiscal_dashboard_data.js": "fiscal_bias",
    }
    if path.name in metric_names:
        metrics = payload.get("metrics")
        value = metrics.get(metric_names[path.name]) if isinstance(metrics, dict) else None
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{path.name} has missing or invalid model inputs.")
    if path.name == "fomc_dashboard_data.js":
        date.fromisoformat(str(payload.get("meeting_date")))
        if payload.get("decision") not in {"raise", "hold", "cut"}:
            raise ValueError("FOMC dashboard is missing its decision.")
    if path.name == "market_dashboard_data.js":
        date.fromisoformat(str(payload.get("target_meeting")))
        providers = payload.get("providers", {})
        if not isinstance(providers, dict):
            raise ValueError("Market dashboard has invalid providers.")
        triplets = [payload.get("blended_probabilities")]
        for provider in ("kalshi", "polymarket"):
            provider_payload = providers.get(provider)
            if not isinstance(provider_payload, dict):
                raise ValueError(f"Market dashboard is missing {provider}.")
            triplets.append(provider_payload.get("probabilities"))
        for triplet in triplets:
            if not isinstance(triplet, dict) or not all(
                isinstance(triplet.get(key), (int, float))
                and math.isfinite(triplet[key]) and triplet[key] >= 0
                for key in ("raise", "hold", "cut")
            ) or sum(triplet[key] for key in ("raise", "hold", "cut")) <= 0:
                raise ValueError("Market dashboard has incomplete probabilities.")
    if path.name == "treasury_dashboard_data.js":
        points = payload.get("points")
        if not isinstance(points, list) or not all(any(
            isinstance(point, dict) and point.get("maturity") == maturity
            and isinstance(point.get("yield_pct"), (int, float))
            and math.isfinite(point["yield_pct"])
            for point in points
        ) for maturity in ("2Y", "10Y")):
            raise ValueError("Treasury dashboard has incomplete yields.")
    graph_key = {
        "kg_dashboard_data.js": "cpi_graph",
        "labor_kg_dashboard_data.js": "labor_graph",
    }.get(path.name)
    if graph_key:
        graph = payload.get(graph_key)
        if not isinstance(graph, dict) or not all(
            isinstance(graph.get(key), list) for key in ("nodes", "edges")
        ):
            raise ValueError(f"{path.name} has an incomplete graph.")
    return payload


def write_payload(payload: object, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_suffix(".js.new")
    staged.write_text(
        f"window.{PAYLOAD_NAMES[path.name]} = "
        + json.dumps(payload, sort_keys=True, indent=2, allow_nan=False) + ";\n",
        encoding="utf-8",
    )
    staged.replace(path)


def retain_payload(payload: object, name: str, data_dir: Path, cache_dir: Path | None) -> None:
    if cache_dir is not None:
        write_payload(payload, cache_dir / "data" / name)
    write_payload(payload, data_dir / name)


def existing_payload(
    name: str, cache_dir: Path | None, fetch, default: dict[str, object] | None,
    *, failures: list[str] | None = None, label: str = "",
    unknown_archives: set[str] | None = None,
) -> dict[str, object] | None:
    if cache_dir is not None and (cache_dir / "data" / name).exists():
        try:
            return read_payload(cache_dir / "data" / name)
        except (OSError, ValueError) as error:
            warn(f"Cached {name} unavailable: {error}")
    try:
        payload = fetch()
        if payload is not None:
            validate_archive(name, payload)
            json.dumps(payload, allow_nan=False)
        return payload if payload is not None else default
    except Exception as error:
        if unknown_archives is not None:
            unknown_archives.add(name)
        if failures is not None:
            failures.append(label)
        warn(f"Previous {name} unavailable: {error}")
        return default


def merge_meetings(*payloads: dict[str, object]) -> dict[str, object]:
    merged: dict[str, object] = {"meetings": []}
    meetings: dict[str, dict[str, object]] = {}
    for payload in payloads:
        validate_archive("fed_rate_history_data.js", payload)
        merged.update(payload)
        for item in payload["meetings"]:
            meeting_date = item["meeting_date"]
            date.fromisoformat(meeting_date)
            meetings[meeting_date] = {**meetings.get(meeting_date, {}), **item}
    merged["meetings"] = [meetings[key] for key in sorted(meetings)]
    if meetings:
        merged["coverage_start"] = min(meetings)
        merged["coverage_end"] = max(meetings)
    return merged


def merge_fomc_history(
    previous: dict[str, object], current: dict[str, object]
) -> dict[str, object]:
    merged = merge_meetings(
        {"meetings": [item for group in previous.values() for item in group]},
        {"meetings": [item for group in current.values() for item in group]},
    )
    grouped: dict[str, object] = {}
    for item in reversed(merged["meetings"]):
        grouped.setdefault(item["meeting_date"][:4], []).append(item)
    return grouped


def run_ingestion_command(
    *,
    label: str,
    module: str,
    arguments: list[str],
    max_attempts: int = MAX_COMMAND_ATTEMPTS,
) -> None:
    command = [sys.executable, "-m", module, *arguments]
    deadline = time.monotonic() + MAX_COMMAND_SECONDS
    for attempt in range(1, max_attempts + 1):
        # A failed attempt must not contribute exports to a later successful one.
        for index, argument in enumerate(arguments):
            if argument in EXPORT_OPTIONS:
                Path(arguments[index + 1]).unlink(missing_ok=True)
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, MAX_COMMAND_SECONDS)
            subprocess.run(command, cwd=REPO_ROOT, check=True, timeout=remaining)
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            remaining = deadline - time.monotonic()
            if attempt == max_attempts or remaining <= 0:
                raise
            delay = RETRY_DELAYS_SECONDS[min(attempt - 1, len(RETRY_DELAYS_SECONDS) - 1)]
            if remaining <= delay:
                raise
            print(
                f"::warning::{label} attempt {attempt}/{max_attempts} failed; "
                f"retrying in {delay} seconds.",
                flush=True,
            )
            time.sleep(delay)


def fetch_existing_history(source_url: str | None) -> dict[str, object]:
    if not source_url:
        return {"snapshots": []}
    request = Request(source_url, headers={"User-Agent": "WhatTheFed/1.0"})
    try:
        with urlopen(request, timeout=20) as response:
            text = response.read().decode("utf-8").strip()
    except HTTPError as error:
        if error.code == 404:
            return {"snapshots": []}
        raise

    prefix = "window.__MODEL_PROBABILITY_HISTORY_DATA__ = "
    if not text.startswith(prefix) or not text.endswith(";"):
        raise RuntimeError("Deployed model probability history has an invalid format.")
    payload = json.loads(text[len(prefix) : -1])
    if not isinstance(payload, dict) or not isinstance(payload.get("snapshots"), list):
        raise RuntimeError("Deployed model probability history has an invalid payload.")
    return payload


def fetch_existing_backtest(source_url: str | None) -> dict[str, object] | None:
    return fetch_existing_meetings(source_url, "__DATA_BACKTEST_DATA__")


def fetch_existing_meetings(
    source_url: str | None, global_name: str
) -> dict[str, object] | None:
    if not source_url:
        return None
    request = Request(source_url, headers={"User-Agent": "WhatTheFed/1.0"})
    try:
        with urlopen(request, timeout=20) as response:
            text = response.read().decode("utf-8").strip()
    except HTTPError as error:
        if error.code == 404:
            return None
        raise

    prefix = f"window.{global_name} = "
    if not text.startswith(prefix) or not text.endswith(";"):
        raise RuntimeError(f"Deployed {global_name} has an invalid format.")
    payload = json.loads(text[len(prefix) : -1])
    if not isinstance(payload, dict) or not isinstance(payload.get("meetings"), list):
        raise RuntimeError(f"Deployed {global_name} has an invalid payload.")
    return payload


def resolve_backtest_payload(
    data_dir: Path,
    existing_backtest: dict[str, object] | None,
    *,
    as_of: date,
) -> dict[str, object]:
    if existing_backtest is not None:
        return existing_backtest
    meetings, histories = load_backtest_inputs(data_dir)
    rate_meetings = [meeting for meeting in meetings if has_valid_target_range(meeting)]
    return build_backtest_payload(meetings=rate_meetings, histories=histories, years=5, as_of=as_of)


def has_valid_target_range(meeting) -> bool:
    lower, upper = meeting.get("target_lower"), meeting.get("target_upper")
    return (
        isinstance(lower, (int, float)) and not isinstance(lower, bool)
        and isinstance(upper, (int, float)) and not isinstance(upper, bool)
        and math.isfinite(lower) and math.isfinite(upper) and lower <= upper
    )


def build_site(
    *,
    output_dir: Path,
    history_source_url: str | None = None,
    backtest_source_url: str | None = None,
    refresh_backtest: bool = False,
    fed_history_source_url: str | None = None,
    refresh_fed_history: bool = False,
    cache_dir: Path | None = None,
    neo4j_env_file: Path | None = None,
) -> None:
    output_dir = output_dir.resolve()
    if REPO_ROOT not in output_dir.parents:
        raise ValueError(f"Output directory must be inside the repository: {output_dir}")
    protected_paths = {".git", ".github", "assets", "config", "data", "scripts", "whatthefed", "tests"}
    if output_dir.relative_to(REPO_ROOT).parts[0] in protected_paths:
        raise ValueError(f"Refusing to replace protected repository path: {output_dir}")
    if cache_dir is not None:
        cache_dir = cache_dir.resolve()
        if (
            REPO_ROOT not in cache_dir.parents
            or cache_dir == output_dir
            or output_dir in cache_dir.parents
            or cache_dir in output_dir.parents
            or cache_dir.relative_to(REPO_ROOT).parts[0] in protected_paths
        ):
            raise ValueError("Cache directory must be inside the repository and outside the output.")
        cache_dir.mkdir(parents=True, exist_ok=True)
    if output_dir.exists():
        shutil.rmtree(output_dir)
    data_dir = output_dir / "data"
    data_dir.mkdir(parents=True)
    failures = []
    unknown_archives = set()
    cached_sources = {}
    if cache_dir is not None:
        for name in EXPECTED_PAYLOADS:
            if name in {"refresh_status_data.js", "meeting_evidence_data.js"}:
                continue
            path = cache_dir / "data" / name
            if path.exists():
                try:
                    payload = read_payload(path)
                    write_payload(payload, data_dir / name)
                    cached_sources[name] = payload
                except (OSError, ValueError) as error:
                    warn(f"Cached {name} unavailable: {error}")

    today = date.today()
    current_year = today.year
    fresh_payloads = set()
    with tempfile.TemporaryDirectory(prefix=".whatthefed-pages-", dir=REPO_ROOT) as temp_dir:
        db_path = (cache_dir or Path(temp_dir)) / "market_snapshots.db"
        commands = [
            (
                "FOMC statements",
                "whatthefed.fomc_ingestion",
                [
                    "--db-path", str(db_path),
                    "--max-meetings", "36",
                    "--dashboard-js", str(data_dir / "fomc_dashboard_data.js"),
                    "--history-js", str(data_dir / "fomc_history_data.js"),
                ],
            ),
            (
                "prediction markets",
                "whatthefed.market_ingestion",
                [
                    "--db-path", str(db_path),
                    "--watchlist", str(REPO_ROOT / "config" / "market_watchlist.json"),
                    "--dashboard-js", str(data_dir / "market_dashboard_data.js"),
                ],
            ),
            (
                "CPI",
                "whatthefed.cpi_ingestion",
                [
                    "--db-path", str(db_path),
                    "--start-year", str(current_year - 7),
                    "--end-year", str(current_year),
                    "--dashboard-js", str(data_dir / "cpi_dashboard_data.js"),
                    "--kg-js", str(data_dir / "kg_dashboard_data.js"),
                ],
            ),
            (
                "labor",
                "whatthefed.labor_ingestion",
                [
                    "--db-path", str(db_path),
                    "--start-year", str(current_year - 7),
                    "--end-year", str(current_year),
                    "--dashboard-js", str(data_dir / "labor_dashboard_data.js"),
                    "--kg-js", str(data_dir / "labor_kg_dashboard_data.js"),
                ],
            ),
            (
                "PPI",
                "whatthefed.ppi_ingestion",
                [
                    "--db-path", str(db_path),
                    "--start-year", str(current_year - 7),
                    "--end-year", str(current_year),
                    "--dashboard-js", str(data_dir / "ppi_dashboard_data.js"),
                ],
            ),
            (
                "Treasury curve",
                "whatthefed.treasury_ingestion",
                [
                    "--db-path", str(db_path),
                    "--start-year", str(current_year - 6),
                    "--end-year", str(current_year),
                    "--dashboard-js", str(data_dir / "treasury_dashboard_data.js"),
                ],
            ),
            (
                "TIPS breakevens",
                "whatthefed.breakeven_ingestion",
                [
                    "--db-path", str(db_path),
                    "--start-year", str(current_year - 6),
                    "--end-year", str(current_year),
                    "--dashboard-js", str(data_dir / "breakeven_dashboard_data.js"),
                ],
            ),
            (
                "NY Fed rates",
                "whatthefed.policy_rates_ingestion",
                [
                    "--db-path", str(db_path),
                    "--start-date", (today - timedelta(days=365 * 6 + 2)).isoformat(),
                    "--end-date", today.isoformat(),
                    "--dashboard-js", str(data_dir / "policy_rate_dashboard_data.js"),
                ],
            ),
            (
                "Treasury fiscal data",
                "whatthefed.fiscal_ingestion",
                [
                    "--db-path", str(db_path),
                    "--start-date", f"{current_year - 7}-01-01",
                    "--dashboard-js", str(data_dir / "fiscal_dashboard_data.js"),
                ],
            ),
            (
                "BEA GDP",
                "whatthefed.gdp_ingestion",
                [
                    "--db-path", str(db_path),
                    "--start-year", str(current_year - 12),
                    "--dashboard-js", str(data_dir / "gdp_dashboard_data.js"),
                ],
            ),
            (
                "Federal Reserve events",
                "whatthefed.fed_events_ingestion",
                [
                    "--start-date", today.isoformat(),
                    "--months", "4",
                    "--dashboard-js", str(data_dir / "fed_events_dashboard_data.js"),
                ],
            ),
        ]
        for label, module, arguments in commands:
            print(f"::group::{label}", flush=True)
            with tempfile.TemporaryDirectory(prefix="source-", dir=temp_dir) as source_dir:
                staged_arguments = list(arguments)
                exports = []
                for index, argument in enumerate(arguments):
                    if argument in EXPORT_OPTIONS:
                        name = Path(arguments[index + 1]).name
                        staged_arguments[index + 1] = str(Path(source_dir) / name)
                        exports.append(name)
                try:
                    run_ingestion_command(label=label, module=module, arguments=staged_arguments)
                    payloads = {name: read_payload(Path(source_dir) / name) for name in exports}
                    if "fomc_history_data.js" in cached_sources and "fomc_history_data.js" in payloads:
                        payloads["fomc_history_data.js"] = merge_fomc_history(
                            cached_sources["fomc_history_data.js"], payloads["fomc_history_data.js"]
                        )
                    for name, payload in payloads.items():
                        retain_payload(payload, name, data_dir, cache_dir)
                    fresh_payloads.update(exports)
                except Exception as error:
                    failures.append(label)
                    warn(f"{label} unavailable; retaining last successful payloads: {error}")
                finally:
                    print("::endgroup::", flush=True)

    for name in EXPECTED_PAYLOADS:
        if not (data_dir / name).exists() and name not in {
            "fed_rate_history_data.js", "data_backtest_data.js", "model_probability_history_data.js",
            "refresh_status_data.js", "meeting_evidence_data.js",
        }:
            write_payload(None, data_dir / name)

    existing_history = existing_payload(
        "model_probability_history_data.js", cache_dir,
        lambda: fetch_existing_history(history_source_url), {"snapshots": []},
        failures=failures, label="Model probability history archive",
        unknown_archives=unknown_archives,
    )
    if "model_probability_history_data.js" not in unknown_archives:
        retain_payload(existing_history, "model_probability_history_data.js", data_dir, cache_dir)
    existing_backtest = existing_payload(
        "data_backtest_data.js", cache_dir,
        lambda: fetch_existing_backtest(backtest_source_url), None,
        failures=failures, label="Backtest archive",
        unknown_archives=unknown_archives,
    )
    if existing_backtest is not None:
        retain_payload(existing_backtest, "data_backtest_data.js", data_dir, cache_dir)
    fed_history = existing_payload(
        "fed_rate_history_data.js", cache_dir,
        lambda: fetch_existing_meetings(fed_history_source_url, "__FED_RATE_HISTORY_DATA__"),
        {"meetings": []},
        failures=failures, label="Fed rate history archive",
        unknown_archives=unknown_archives,
    )
    if refresh_fed_history:
        try:
            refreshed = fetch_history_payload(start_year=1982)
            validate_archive("fed_rate_history_data.js", refreshed)
            if not refreshed["meetings"] or not all(
                has_valid_target_range(meeting) for meeting in refreshed["meetings"]
            ):
                raise ValueError("Refreshed Fed rate history has incomplete target ranges.")
            fed_history = merge_meetings(fed_history, refreshed)
            unknown_archives.discard("fed_rate_history_data.js")
        except Exception as error:
            failures.append("Fed rate history")
            warn(f"Fed rate history refresh failed; retaining archive: {error}")
    if not fed_history["meetings"]:
        warn("Fed rate history unavailable; historical views are disabled.")
    if "fed_rate_history_data.js" not in unknown_archives:
        retain_payload(fed_history, "fed_rate_history_data.js", data_dir, cache_dir)

    backtest = existing_backtest or {"meetings": []}
    try:
        if refresh_backtest or existing_backtest is None:
            if fed_history["meetings"]:
                backtest = resolve_backtest_payload(data_dir, None, as_of=today)
        validate_archive("data_backtest_data.js", backtest)
        if backtest["meetings"]:
            unknown_archives.discard("data_backtest_data.js")
        if "data_backtest_data.js" not in unknown_archives:
            retain_payload(backtest, "data_backtest_data.js", data_dir, cache_dir)
    except Exception as error:
        failures.append("Backtest")
        warn(f"Backtest unavailable; retaining previous results: {error}")
        if "data_backtest_data.js" not in unknown_archives:
            retain_payload(existing_backtest or {"meetings": []}, "data_backtest_data.js", data_dir, cache_dir)

    try:
        if "model_probability_history_data.js" in unknown_archives:
            raise RuntimeError("Previous probability history is unknown; cannot start a replacement archive.")
        required = set(EXPECTED_PAYLOADS) - {
            "fed_rate_history_data.js", "data_backtest_data.js",
            "model_probability_history_data.js", "refresh_status_data.js", "meeting_evidence_data.js",
        }
        if not required.issubset(fresh_payloads):
            raise RuntimeError("Required sources were not all refreshed successfully.")
        snapshot = build_prediction_snapshot(load_dashboard_payloads(data_dir))
        history = merge_history(existing_history, snapshot)
        validate_archive("model_probability_history_data.js", history)
        retain_payload(history, "model_probability_history_data.js", data_dir, cache_dir)
    except Exception as error:
        failures.append("Model snapshot")
        warn(f"Model snapshot skipped; preserving probability history: {error}")

    write_payload(
        {"generated_at": datetime.now(timezone.utc).isoformat(), "failures": list(dict.fromkeys(failures))},
        data_dir / "refresh_status_data.js",
    )

    if unknown_archives:
        raise RuntimeError(
            "Deployment withheld to preserve unavailable deployed archives: "
            + ", ".join(sorted(unknown_archives))
        )

    if neo4j_env_file is not None or os.environ.get("NEO4J_URI"):
        evidence = build_neo4j_evidence(data_dir, load_settings(neo4j_env_file))
    else:
        evidence = {
            "available": False,
            "reason": "Neo4j evidence is not configured for this build.",
        }
    export_evidence_js(evidence, data_dir / "meeting_evidence_data.js")

    missing = [
        name
        for name in EXPECTED_PAYLOADS
        if not (data_dir / name).is_file() or (data_dir / name).stat().st_size < 20
    ]
    if missing:
        raise RuntimeError(f"Static build did not produce required payloads: {missing}")

    shutil.copy2(REPO_ROOT / "index.html", output_dir / "index.html")
    assets_dir = output_dir / "assets"
    assets_dir.mkdir()
    shutil.copy2(REPO_ROOT / "assets" / "meeting-evidence.js", assets_dir / "meeting-evidence.js")
    (output_dir / ".nojekyll").write_text("", encoding="utf-8")
    database_files = [
        path
        for path in output_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}
    ]
    if database_files:
        raise RuntimeError("Static site artifact unexpectedly contains a database.")

    payload_bytes = sum((data_dir / name).stat().st_size for name in EXPECTED_PAYLOADS)
    print(
        f"Built {output_dir} with {len(EXPECTED_PAYLOADS)} payloads "
        f"({payload_bytes / 1024:.1f} KiB); SQLite database "
        f"{'retained in cache' if cache_dir else 'discarded'}."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="_site", type=Path)
    parser.add_argument("--cache-dir", type=Path, help="Durable local ingestion database and payload cache.")
    parser.add_argument(
        "--neo4j-env-file", type=Path,
        help="Local ignored Neo4j credentials file. Hosted builds use environment secrets.",
    )
    parser.add_argument(
        "--history-source-url",
        help="Previously deployed model history to extend. A missing file starts a new archive.",
    )
    parser.add_argument(
        "--backtest-source-url",
        help="Previously deployed backtest to reuse unless --refresh-backtest is set.",
    )
    parser.add_argument(
        "--fed-history-source-url",
        help="Previously deployed Fed rate history to reuse without fetching FRED.",
    )
    parser.add_argument(
        "--refresh-fed-history",
        action="store_true",
        help="Rebuild historical Fed rates from the Fed and FRED (may time out).",
    )
    parser.add_argument(
        "--refresh-backtest",
        action="store_true",
        help="Recalculate the backtest instead of reusing the deployed payload.",
    )
    args = parser.parse_args(argv)
    build_site(
        output_dir=args.output_dir,
        cache_dir=args.cache_dir,
        history_source_url=args.history_source_url,
        backtest_source_url=args.backtest_source_url,
        refresh_backtest=args.refresh_backtest,
        fed_history_source_url=args.fed_history_source_url,
        refresh_fed_history=args.refresh_fed_history,
        neo4j_env_file=args.neo4j_env_file,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
