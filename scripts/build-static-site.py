"""Build the complete static dashboard using a disposable SQLite database."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from whatthefed.prediction_history import (
    build_prediction_snapshot,
    export_history_js,
    load_dashboard_payloads,
    merge_history,
)

EXPECTED_PAYLOADS = (
    "fomc_dashboard_data.js",
    "fomc_history_data.js",
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
)
MAX_COMMAND_ATTEMPTS = 3
RETRY_DELAYS_SECONDS = (10, 30)


def run_ingestion_command(
    *,
    label: str,
    module: str,
    arguments: list[str],
    max_attempts: int = MAX_COMMAND_ATTEMPTS,
) -> None:
    command = [sys.executable, "-m", module, *arguments]
    for attempt in range(1, max_attempts + 1):
        try:
            subprocess.run(command, cwd=REPO_ROOT, check=True)
            return
        except subprocess.CalledProcessError:
            if attempt == max_attempts:
                raise
            delay = RETRY_DELAYS_SECONDS[min(attempt - 1, len(RETRY_DELAYS_SECONDS) - 1)]
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


def build_site(*, output_dir: Path, history_source_url: str | None = None) -> None:
    output_dir = output_dir.resolve()
    if REPO_ROOT not in output_dir.parents:
        raise ValueError(f"Output directory must be inside the repository: {output_dir}")
    if output_dir.name in {
        "data",
        "scripts",
        "whatthefed",
        "tests",
    }:
        raise ValueError(f"Refusing to replace protected repository path: {output_dir}")
    existing_history = fetch_existing_history(history_source_url)
    if output_dir.exists():
        shutil.rmtree(output_dir)
    data_dir = output_dir / "data"
    data_dir.mkdir(parents=True)

    today = date.today()
    current_year = today.year
    with tempfile.TemporaryDirectory(prefix="whatthefed-pages-") as temp_dir:
        db_path = Path(temp_dir) / "market_snapshots.db"
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
                    "--start-year", str(current_year - 4),
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
                    "--start-year", str(current_year - 4),
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
                    "--start-year", str(current_year - 4),
                    "--end-year", str(current_year),
                    "--dashboard-js", str(data_dir / "ppi_dashboard_data.js"),
                ],
            ),
            (
                "Treasury curve",
                "whatthefed.treasury_ingestion",
                [
                    "--db-path", str(db_path),
                    "--year", str(current_year),
                    "--dashboard-js", str(data_dir / "treasury_dashboard_data.js"),
                ],
            ),
            (
                "TIPS breakevens",
                "whatthefed.breakeven_ingestion",
                [
                    "--db-path", str(db_path),
                    "--year", str(current_year),
                    "--dashboard-js", str(data_dir / "breakeven_dashboard_data.js"),
                ],
            ),
            (
                "NY Fed rates",
                "whatthefed.policy_rates_ingestion",
                [
                    "--db-path", str(db_path),
                    "--start-date", (today - timedelta(days=900)).isoformat(),
                    "--end-date", today.isoformat(),
                    "--dashboard-js", str(data_dir / "policy_rate_dashboard_data.js"),
                ],
            ),
            (
                "Treasury fiscal data",
                "whatthefed.fiscal_ingestion",
                [
                    "--db-path", str(db_path),
                    "--start-date", f"{current_year - 4}-01-01",
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
            run_ingestion_command(label=label, module=module, arguments=arguments)
            print("::endgroup::", flush=True)

    snapshot = build_prediction_snapshot(load_dashboard_payloads(data_dir))
    history = merge_history(existing_history, snapshot)
    export_history_js(history, data_dir / "model_probability_history_data.js")

    missing = [
        name
        for name in EXPECTED_PAYLOADS
        if not (data_dir / name).is_file() or (data_dir / name).stat().st_size < 20
    ]
    if missing:
        raise RuntimeError(f"Static build did not produce required payloads: {missing}")

    shutil.copy2(REPO_ROOT / "index.html", output_dir / "index.html")
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
        f"({payload_bytes / 1024:.1f} KiB); SQLite database discarded."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="_site", type=Path)
    parser.add_argument(
        "--history-source-url",
        help="Previously deployed model history to extend. A missing file starts a new archive.",
    )
    args = parser.parse_args(argv)
    build_site(output_dir=args.output_dir, history_source_url=args.history_source_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
