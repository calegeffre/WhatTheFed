"""Build the complete static dashboard using a disposable SQLite database."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
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
)


def build_site(*, output_dir: Path) -> None:
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
        ]
        for label, module, arguments in commands:
            print(f"::group::{label}", flush=True)
            subprocess.run(
                [sys.executable, "-m", module, *arguments],
                cwd=REPO_ROOT,
                check=True,
            )
            print("::endgroup::", flush=True)

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
    args = parser.parse_args(argv)
    build_site(output_dir=args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
