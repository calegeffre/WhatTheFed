"""Ingestion of U.S. Treasury TIPS real yields and derived breakeven inflation.

Breakeven inflation (nominal Treasury yield minus the matched-maturity TIPS real
yield) is the market's forward-looking inflation expectation. The FOMC leans on it
heavily as its "are expectations still anchored?" check, which makes it a different
class of signal from the backward-looking realized CPI prints.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib.request import Request, urlopen

from .rag import Document


DEFAULT_DB_PATH = Path("data") / "market_snapshots.db"
PROVIDER = "US Treasury"
TREASURY_BASE_URL = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv"
DEFAULT_REAL_YIELD_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "TextView?type=daily_treasury_real_yield_curve"
)

# The real-yield feed spells tenors "5 YR" (uppercase) unlike the nominal feed's "5 Yr".
REAL_YIELD_COLUMNS: dict[str, tuple[str, str, float]] = {
    "5 YR": ("TIPS5Y", "5Y", 60.0),
    "7 YR": ("TIPS7Y", "7Y", 84.0),
    "10 YR": ("TIPS10Y", "10Y", 120.0),
    "20 YR": ("TIPS20Y", "20Y", 240.0),
    "30 YR": ("TIPS30Y", "30Y", 360.0),
}
MATURITY_MONTHS: dict[str, float] = {
    maturity: months for _, (_, maturity, months) in REAL_YIELD_COLUMNS.items()
}
# Nominal counterpart symbols written by treasury_ingestion.
NOMINAL_SYMBOL_BY_MATURITY = {"5Y": "UST5Y", "7Y": "UST7Y", "10Y": "UST10Y", "20Y": "UST20Y", "30Y": "UST30Y"}

# The FOMC targets 2% PCE inflation, but breakevens are priced off CPI, which has
# historically run ~0.30pp hotter. 2.30% CPI-breakeven is therefore "on target".
NEUTRAL_BREAKEVEN_PCT = 2.30
BREAKEVEN_SATURATION_PCT = 0.50


class BreakevenIngestionError(RuntimeError):
    pass


GetText = Callable[[str], str]


@dataclass(frozen=True)
class RealYieldPoint:
    snapshot_at: str
    symbol: str
    label: str
    maturity: str
    real_yield_pct: float | None
    change_value: float | None
    source_url: str
    provider: str = PROVIDER


class TreasuryRealYieldClient:
    """Reads the U.S. Treasury daily TIPS real yield curve CSV feed."""

    def __init__(self, *, get_text: GetText | None = None, base_url: str = TREASURY_BASE_URL) -> None:
        self.get_text = get_text or _get_text
        self.base_url = base_url

    def build_url(self, year: int) -> str:
        return (
            f"{self.base_url}/{year}/all"
            f"?type=daily_treasury_real_yield_curve&field_tdr_date_value={year}&page&_format=csv"
        )

    def fetch_points(self, *, year: int) -> tuple[str, list[RealYieldPoint], str]:
        source_url = self.build_url(year)
        text = self.get_text(source_url)
        points = self._parse_csv(text, source_url)
        if not points:
            raise BreakevenIngestionError(f"Treasury real yield feed returned no usable rows for {year}.")
        return max(point.snapshot_at for point in points), points, source_url

    def _parse_csv(self, text: str, source_url: str) -> list[RealYieldPoint]:
        reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
        if not reader.fieldnames or "Date" not in reader.fieldnames:
            raise BreakevenIngestionError(
                "Treasury real yield CSV did not include a Date column; the endpoint format may have changed."
            )

        by_symbol: dict[str, list[tuple[str, float]]] = {}
        meta: dict[str, tuple[str, str]] = {}
        for row in reader:
            snapshot_at = _normalize_date(row.get("Date"))
            if snapshot_at is None:
                continue
            for column, (symbol, maturity, _months) in REAL_YIELD_COLUMNS.items():
                if column not in row:
                    continue
                value = _coerce_float(row.get(column))
                if value is None:
                    continue
                by_symbol.setdefault(symbol, []).append((snapshot_at, value))
                meta[symbol] = (f"{maturity} TIPS real yield", maturity)

        points: list[RealYieldPoint] = []
        for symbol, series in by_symbol.items():
            series.sort(key=lambda item: item[0])
            label, maturity = meta[symbol]
            previous: float | None = None
            for snapshot_at, value in series:
                points.append(
                    RealYieldPoint(
                        snapshot_at=snapshot_at,
                        symbol=symbol,
                        label=label,
                        maturity=maturity,
                        real_yield_pct=value,
                        change_value=None if previous is None else round(value - previous, 4),
                        source_url=source_url,
                    )
                )
                previous = value

        points.sort(key=lambda item: (item.snapshot_at, MATURITY_MONTHS.get(item.maturity, 9999.0)))
        return points


class RealYieldStore:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._open_connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS real_yield_observations (
                    snapshot_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    label TEXT NOT NULL,
                    maturity TEXT NOT NULL,
                    real_yield_pct REAL,
                    change_value REAL,
                    source_url TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (snapshot_at, symbol)
                );
                CREATE INDEX IF NOT EXISTS idx_real_yield_snapshot
                ON real_yield_observations (snapshot_at DESC);

                CREATE TABLE IF NOT EXISTS breakeven_ingestion_runs (
                    run_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    observation_count INTEGER,
                    snapshot_at TEXT,
                    source_url TEXT,
                    error_message TEXT
                );
                """
            )

    def write_observations(self, observations: Iterable[RealYieldPoint]) -> int:
        self.initialize()
        values = list(observations)
        if not values:
            return 0
        fetched_at = _utcnow().isoformat()
        with self._open_connection() as connection:
            connection.executemany(
                """
                INSERT OR REPLACE INTO real_yield_observations (
                    snapshot_at, symbol, label, maturity, real_yield_pct, change_value,
                    source_url, provider, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.snapshot_at,
                        item.symbol,
                        item.label,
                        item.maturity,
                        item.real_yield_pct,
                        item.change_value,
                        item.source_url,
                        item.provider,
                        fetched_at,
                    )
                    for item in values
                ],
            )
        return len(values)

    def record_run(
        self,
        *,
        run_id: str,
        started_at: str,
        status: str,
        completed_at: str | None = None,
        observation_count: int | None = None,
        snapshot_at: str | None = None,
        source_url: str | None = None,
        error_message: str | None = None,
    ) -> None:
        self.initialize()
        with self._open_connection() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO breakeven_ingestion_runs (
                    run_id, started_at, completed_at, status, observation_count,
                    snapshot_at, source_url, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, started_at, completed_at, status, observation_count, snapshot_at, source_url, error_message),
            )

    def load_documents(self, *, per_symbol_limit: int = 12) -> list[Document]:
        self.initialize()
        with self._open_connection() as connection:
            rows = connection.execute(
                """
                WITH ranked AS (
                    SELECT symbol, label, maturity, snapshot_at, real_yield_pct, change_value, source_url,
                           ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY snapshot_at DESC) AS row_rank
                    FROM real_yield_observations
                )
                SELECT symbol, label, maturity, snapshot_at, real_yield_pct, change_value, source_url
                FROM ranked
                WHERE row_rank <= ?
                ORDER BY symbol ASC, snapshot_at DESC
                """,
                (per_symbol_limit,),
            ).fetchall()

        docs: list[Document] = []
        for row in rows:
            snapshot_at = str(row["snapshot_at"])
            symbol = str(row["symbol"])
            real_yield = _coerce_float(row["real_yield_pct"])
            change_value = _coerce_float(row["change_value"])
            docs.append(
                Document(
                    source=f"real_yield_{symbol}_{snapshot_at.replace('-', '')}",
                    content=(
                        f"{row['label']} ({symbol}, {row['maturity']}) on {snapshot_at}: "
                        f"real yield {real_yield:.3f}%, change {change_value:+.3f}."
                        if real_yield is not None and change_value is not None
                        else f"{row['label']} ({symbol}) on {snapshot_at}: real yield "
                        f"{'n/a' if real_yield is None else format(real_yield, '.3f') + '%'}."
                    ),
                    kind="real_yield_observation",
                    published_at=snapshot_at,
                    source_url=str(row["source_url"]),
                    metadata={
                        "series_id": symbol,
                        "series_label": str(row["label"]),
                        "category": "breakeven",
                        "maturity": str(row["maturity"]),
                        "observation_date": snapshot_at,
                        "value": real_yield,
                        "provider": PROVIDER,
                    },
                )
            )
        return docs

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    @contextlib.contextmanager
    def _open_connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()


class BreakevenIngestionService:
    def __init__(self, *, store: RealYieldStore, client: TreasuryRealYieldClient | None = None) -> None:
        self.store = store
        self.client = client or TreasuryRealYieldClient()

    def ingest(self, *, years: Iterable[int] | None = None) -> dict[str, object]:
        run_id = str(uuid.uuid4())
        started_at = _utcnow().isoformat()
        self.store.record_run(run_id=run_id, started_at=started_at, status="started")
        target_years = sorted({int(year) for year in (years or [_utcnow().year])})
        try:
            observation_count = 0
            snapshots: list[str] = []
            source_url = ""
            for year in target_years:
                snapshot_at, points, url = self.client.fetch_points(year=year)
                observation_count += self.store.write_observations(points)
                snapshots.append(snapshot_at)
                source_url = url
            latest = max(snapshots) if snapshots else None
            self.store.record_run(
                run_id=run_id,
                started_at=started_at,
                completed_at=_utcnow().isoformat(),
                status="completed",
                observation_count=observation_count,
                snapshot_at=latest,
                source_url=source_url,
            )
            return {
                "run_id": run_id,
                "observation_count": observation_count,
                "snapshot_at": latest,
                "source_url": source_url,
                "years": target_years,
            }
        except Exception as exc:
            self.store.record_run(
                run_id=run_id,
                started_at=started_at,
                completed_at=_utcnow().isoformat(),
                status="failed",
                error_message=str(exc),
            )
            raise


def breakeven_bias(breakeven_10y_pct: float | None) -> float:
    """Map 10-year breakeven inflation onto the shared [-1, +1] bias scale.

    Expectations drifting above the CPI-equivalent of the 2% PCE target read
    hawkish; drifting below reads dovish.
    """
    if breakeven_10y_pct is None:
        return 0.0
    raw = (breakeven_10y_pct - NEUTRAL_BREAKEVEN_PCT) / BREAKEVEN_SATURATION_PCT
    return round(max(-1.0, min(1.0, raw)), 4)


def build_breakeven_series(*, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, dict[str, float]]:
    """Nominal minus real yield per date and maturity, keyed date -> maturity -> pct."""
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        real_rows = connection.execute(
            """
            SELECT snapshot_at, maturity, real_yield_pct
            FROM real_yield_observations
            WHERE real_yield_pct IS NOT NULL
            """
        ).fetchall()
        nominal_rows = connection.execute(
            """
            SELECT snapshot_at, maturity, yield_pct
            FROM treasury_observations
            WHERE yield_pct IS NOT NULL AND symbol IN ('UST5Y','UST7Y','UST10Y','UST20Y','UST30Y')
            """
        ).fetchall()
    except sqlite3.DatabaseError:
        return {}
    finally:
        connection.close()

    real: dict[str, dict[str, float]] = {}
    for row in real_rows:
        value = _coerce_float(row["real_yield_pct"])
        if value is not None:
            real.setdefault(str(row["snapshot_at"]), {})[str(row["maturity"])] = value

    nominal: dict[str, dict[str, float]] = {}
    for row in nominal_rows:
        value = _coerce_float(row["yield_pct"])
        if value is not None:
            nominal.setdefault(str(row["snapshot_at"]), {})[str(row["maturity"])] = value

    breakevens: dict[str, dict[str, float]] = {}
    for snapshot_at, real_by_maturity in real.items():
        nominal_by_maturity = nominal.get(snapshot_at)
        if not nominal_by_maturity:
            continue
        for maturity, real_value in real_by_maturity.items():
            nominal_value = nominal_by_maturity.get(maturity)
            if nominal_value is None:
                continue
            breakevens.setdefault(snapshot_at, {})[maturity] = round(nominal_value - real_value, 4)
    return breakevens


def build_breakeven_bias_history(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    limit: int = 400,
) -> list[dict[str, object]]:
    """Daily breakeven-derived bias, oldest first, already in bias units."""
    breakevens = build_breakeven_series(db_path=db_path)
    history: list[dict[str, object]] = []
    for snapshot_at in sorted(breakevens):
        by_maturity = breakevens[snapshot_at]
        ten_year = by_maturity.get("10Y")
        if ten_year is None:
            continue
        history.append(
            {
                "date": snapshot_at,
                "bias": breakeven_bias(ten_year),
                "breakeven_5y": by_maturity.get("5Y"),
                "breakeven_10y": ten_year,
                "breakeven_30y": by_maturity.get("30Y"),
            }
        )
    return history[-max(1, limit) :]


def build_dashboard_breakeven_payload(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    row_limit: int = 64,
) -> dict[str, object] | None:
    breakevens = build_breakeven_series(db_path=db_path)
    if not breakevens:
        return None
    latest_date = max(breakevens)
    by_maturity = breakevens[latest_date]
    ten_year = by_maturity.get("10Y")

    metrics: dict[str, float] = {}
    for maturity, value in by_maturity.items():
        metrics[f"breakeven_{maturity.lower()}"] = value
    bias = breakeven_bias(ten_year)
    metrics["breakeven_bias"] = bias
    metrics["breakeven_heat_score"] = float(_score_from_bias(bias))
    five_year = by_maturity.get("5Y")
    if five_year is not None and ten_year is not None:
        # A 10y above 5y means the market expects inflation pressure to build later.
        metrics["breakeven_term_slope"] = round(ten_year - five_year, 4)

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        real_rows = connection.execute(
            """
            SELECT symbol, label, maturity, real_yield_pct
            FROM real_yield_observations
            WHERE snapshot_at = ?
            ORDER BY symbol ASC
            """,
            (latest_date,),
        ).fetchall()
    finally:
        connection.close()

    latest_values = []
    for row in real_rows:
        maturity = str(row["maturity"])
        latest_values.append(
            {
                "series_id": str(row["symbol"]),
                "label": str(row["label"]),
                "category": "breakeven",
                "maturity": maturity,
                "real_yield_pct": _coerce_float(row["real_yield_pct"]),
                "breakeven_pct": by_maturity.get(maturity),
                "nominal_symbol": NOMINAL_SYMBOL_BY_MATURITY.get(maturity),
            }
        )

    return {
        "generated_at": _utcnow().isoformat(),
        "metric_date": latest_date,
        "provider": PROVIDER,
        "metrics": metrics,
        "metric_metadata": {
            "breakeven_bias": {
                "formula": "clamp((breakeven_10y - 2.30) / 0.50, -1, 1)",
                "note": (
                    "Breakevens price CPI, which runs about 0.30pp above the PCE measure the "
                    "FOMC targets, so 2.30% CPI-breakeven is treated as on-target."
                ),
                "neutral_assumptions": {"breakeven_10y_pct": NEUTRAL_BREAKEVEN_PCT},
            }
        },
        "heat_card": _build_breakeven_heat_card(metric_date=latest_date, metrics=metrics),
        "latest_values": latest_values[: max(1, row_limit)],
        "bias_history": build_breakeven_bias_history(db_path=db_path),
        "source_url": DEFAULT_REAL_YIELD_URL,
    }


def _build_breakeven_heat_card(*, metric_date: str, metrics: dict[str, float]) -> dict[str, object]:
    bias = float(metrics.get("breakeven_bias", 0.0))
    score = _score_from_bias(bias)
    sign = "+" if bias > 0 else ""
    pills = [f"TIPS {metric_date}", f"bias {sign}{bias:.2f}"]
    if "breakeven_10y" in metrics:
        pills.append(f"10y breakeven {metrics['breakeven_10y']:.2f}%")
    if "breakeven_5y" in metrics:
        pills.append(f"5y {metrics['breakeven_5y']:.2f}%")
    if "breakeven_30y" in metrics:
        pills.append(f"30y {metrics['breakeven_30y']:.2f}%")
    return {
        "label": "Inflation Expectations",
        "display": f"{sign}{bias:.2f}",
        "score": score,
        "tone": _tone_from_score(score),
        "toneLabel": "breakevens",
        "sources": pills,
    }


def build_breakeven_knowledge_graph_payload(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    per_maturity_limit: int = 8,
) -> dict[str, object] | None:
    breakevens = build_breakeven_series(db_path=db_path)
    if not breakevens:
        return None

    dates = sorted(breakevens)
    recent = dates[-max(1, per_maturity_limit) :]
    maturities = sorted(
        {maturity for values in breakevens.values() for maturity in values},
        key=lambda item: MATURITY_MONTHS.get(item, 9999.0),
    )

    nodes: list[dict[str, object]] = [
        {
            "id": "breakeven-hub",
            "kind": "breakeven_hub",
            "label": "Inflation Expectations",
            "sublabel": "TIPS breakevens",
            "x": 0.0,
            "y": 4.2,
            "z": -3.4,
            "size": 0.24,
            "color": "#ffa657",
        }
    ]
    edges: list[dict[str, object]] = [
        {"source": "breakeven-hub", "target": "fomc-hub", "color": "#ffa657", "opacity": 0.3}
    ]

    obs_count = 0
    for index, maturity in enumerate(maturities):
        series_id = f"breakeven-series-{maturity.lower()}"
        series_x = -3.0 + index * 1.5
        nodes.append(
            {
                "id": series_id,
                "kind": "breakeven_series",
                "label": f"{maturity} breakeven",
                "sublabel": "nominal - TIPS real",
                "x": round(series_x, 3),
                "y": 3.3,
                "z": -3.4,
                "size": 0.14,
                "color": "#ffa657",
            }
        )
        edges.append({"source": "breakeven-hub", "target": series_id, "color": "#ffa657", "opacity": 0.28})

        previous_obs_id = None
        for depth, snapshot_at in enumerate(recent):
            value = breakevens.get(snapshot_at, {}).get(maturity)
            if value is None:
                continue
            obs_id = f"breakeven-{maturity.lower()}-{snapshot_at}"
            obs_count += 1
            nodes.append(
                {
                    "id": obs_id,
                    "kind": "breakeven_observation",
                    "label": snapshot_at,
                    "sublabel": f"{value:.2f}%",
                    "x": round(series_x, 3),
                    "y": round(3.3 - 0.3 * (depth + 1), 3),
                    "z": round(-3.4 + 0.3 * (depth + 1), 3),
                    "size": 0.055 if depth < len(recent) - 1 else 0.085,
                    "color": "#ffa657",
                }
            )
            edges.append({"source": series_id, "target": obs_id, "color": "#ffa657", "opacity": 0.1})
            if previous_obs_id is not None:
                edges.append({"source": previous_obs_id, "target": obs_id, "color": "#ffa657", "opacity": 0.22})
            previous_obs_id = obs_id

    return {
        "generated_at": _utcnow().isoformat(),
        "breakeven_graph": {
            "nodes": nodes,
            "edges": edges,
            "stats": {
                "series_count": len(maturities),
                "observation_count": obs_count,
                "edge_count": len(edges),
            },
        },
    }


def export_dashboard_breakeven_js(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    output_js_path: str | Path,
    per_maturity_points: int = 8,
    raw_row_limit: int = 64,
) -> dict[str, object] | None:
    payload = build_dashboard_breakeven_payload(db_path=db_path, row_limit=raw_row_limit)
    if payload is None:
        return None
    graph_payload = build_breakeven_knowledge_graph_payload(
        db_path=db_path, per_maturity_limit=per_maturity_points
    )
    if graph_payload:
        payload = {**payload, **graph_payload}
    output_path = Path(output_js_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        "window.__BREAKEVEN_DASHBOARD_DATA__ = " + json.dumps(payload, indent=2, sort_keys=True) + ";\n",
        encoding="utf-8",
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest U.S. Treasury TIPS real yields and derive breakeven inflation expectations."
    )
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="SQLite database path.")
    parser.add_argument(
        "--year",
        type=int,
        action="append",
        dest="years",
        help="Calendar year to ingest. Repeat to backfill multiple years (default: current year).",
    )
    parser.add_argument("--start-year", type=int, help="Start year for a contiguous backfill range.")
    parser.add_argument("--end-year", type=int, help="End year for a contiguous backfill range.")
    parser.add_argument("--dashboard-js", help="Optional output path for the dashboard payload.")
    parser.add_argument(
        "--kg-points-per-maturity",
        type=int,
        default=8,
        help="Number of most-recent dates per maturity in the graph payload.",
    )
    parser.add_argument(
        "--raw-row-limit",
        type=int,
        default=64,
        help="Max rows to include in the Raw Data table payload.",
    )
    args = parser.parse_args(argv)

    years: set[int] = set(args.years or [])
    if args.start_year is not None:
        end_year = args.end_year or _utcnow().year
        if end_year < args.start_year:
            parser.error("--end-year must be greater than or equal to --start-year")
        years.update(range(args.start_year, end_year + 1))
    if not years:
        years = {_utcnow().year}

    service = BreakevenIngestionService(store=RealYieldStore(args.db_path))
    result = service.ingest(years=sorted(years))
    if args.dashboard_js:
        export_dashboard_breakeven_js(
            db_path=args.db_path,
            output_js_path=args.dashboard_js,
            per_maturity_points=args.kg_points_per_maturity,
            raw_row_limit=args.raw_row_limit,
        )

    print(
        f"Ingested {result['observation_count']} TIPS real yield points across {result['years']} "
        f"(latest snapshot_at={result['snapshot_at']})."
    )
    return 0


def _normalize_date(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _score_from_bias(bias: float) -> int:
    if bias >= 0.75:
        return 5
    if bias >= 0.25:
        return 4
    if bias > -0.25:
        return 3
    if bias >= -0.75:
        return 2
    return 1


def _tone_from_score(score: int) -> str:
    if score >= 5:
        return "hot"
    if score == 4:
        return "warm"
    if score == 3:
        return "balanced"
    if score == 2:
        return "cool"
    return "cold"


def _coerce_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _get_text(url: str) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": "WhatTheFed/1.0 (+https://github.com/calegeffre/WhatTheFed)",
            "Accept": "text/csv,text/plain,*/*",
        },
        method="GET",
    )
    with urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8", errors="replace")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


if __name__ == "__main__":
    raise SystemExit(main())
