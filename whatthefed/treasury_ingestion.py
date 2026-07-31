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
from typing import Callable, Iterable, Mapping
from urllib.request import Request, urlopen

from .rag import Document


DEFAULT_DB_PATH = Path("data") / "market_snapshots.db"
PROVIDER = "US Treasury"
TREASURY_BASE_URL = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv"
DEFAULT_TREASURY_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "TextView?type=daily_treasury_yield_curve"
)

# Column header -> (symbol, maturity label, months). The feed mixes "1 Mo" and "1.5 Month"
# spellings, so map explicitly rather than inferring with a regex.
MATURITY_COLUMNS: dict[str, tuple[str, str, float]] = {
    "1 Mo": ("UST1M", "1M", 1.0),
    "1.5 Month": ("UST1_5M", "1.5M", 1.5),
    "2 Mo": ("UST2M", "2M", 2.0),
    "3 Mo": ("UST3M", "3M", 3.0),
    "4 Mo": ("UST4M", "4M", 4.0),
    "6 Mo": ("UST6M", "6M", 6.0),
    "1 Yr": ("UST1Y", "1Y", 12.0),
    "2 Yr": ("UST2Y", "2Y", 24.0),
    "3 Yr": ("UST3Y", "3Y", 36.0),
    "5 Yr": ("UST5Y", "5Y", 60.0),
    "7 Yr": ("UST7Y", "7Y", 84.0),
    "10 Yr": ("UST10Y", "10Y", 120.0),
    "20 Yr": ("UST20Y", "20Y", 240.0),
    "30 Yr": ("UST30Y", "30Y", 360.0),
}
MATURITY_MONTHS: dict[str, float] = {
    maturity: months for _, (_, maturity, months) in MATURITY_COLUMNS.items()
}


@dataclass(frozen=True)
class TreasuryPoint:
    snapshot_at: str
    symbol: str
    label: str
    maturity: str
    yield_pct: float | None
    price: float | None
    change_value: float | None
    source_url: str
    provider: str = PROVIDER


GetText = Callable[[str], str]


class TreasuryIngestionError(RuntimeError):
    pass


class TreasuryParYieldClient:
    """Reads the U.S. Treasury daily par yield curve CSV feed.

    Unlike the previous Bloomberg scrape, this endpoint is public, unauthenticated,
    and returns a full year of daily observations in a single request.
    """

    def __init__(
        self,
        *,
        get_text: GetText | None = None,
        base_url: str = TREASURY_BASE_URL,
    ) -> None:
        self.get_text = get_text or _get_text
        self.base_url = base_url

    def build_url(self, year: int) -> str:
        return (
            f"{self.base_url}/{year}/all"
            f"?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv"
        )

    def fetch_points(self, *, year: int) -> tuple[str, list[TreasuryPoint], str]:
        source_url = self.build_url(year)
        text = self.get_text(source_url)
        points = self._parse_csv(text, source_url)
        if not points:
            raise TreasuryIngestionError(f"Treasury feed returned no usable rows for {year}.")
        latest_snapshot = max(point.snapshot_at for point in points)
        return latest_snapshot, points, source_url

    def _parse_csv(self, text: str, source_url: str) -> list[TreasuryPoint]:
        reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
        if not reader.fieldnames or "Date" not in reader.fieldnames:
            raise TreasuryIngestionError(
                "Treasury CSV feed did not include a Date column; the endpoint format may have changed."
            )

        # Collect per-symbol series first so day-over-day changes can be derived.
        by_symbol: dict[str, list[tuple[str, float]]] = {}
        meta: dict[str, tuple[str, str]] = {}
        for row in reader:
            snapshot_at = _normalize_date(row.get("Date"))
            if snapshot_at is None:
                continue
            for column, (symbol, maturity, _months) in MATURITY_COLUMNS.items():
                if column not in row:
                    continue
                value = _coerce_float(row.get(column))
                if value is None:
                    continue
                by_symbol.setdefault(symbol, []).append((snapshot_at, value))
                meta[symbol] = (f"{maturity} Treasury", maturity)

        points: list[TreasuryPoint] = []
        for symbol, series in by_symbol.items():
            series.sort(key=lambda item: item[0])
            label, maturity = meta[symbol]
            previous: float | None = None
            for snapshot_at, value in series:
                points.append(
                    TreasuryPoint(
                        snapshot_at=snapshot_at,
                        symbol=symbol,
                        label=label,
                        maturity=maturity,
                        yield_pct=value,
                        price=None,
                        change_value=None if previous is None else round(value - previous, 4),
                        source_url=source_url,
                    )
                )
                previous = value

        points.sort(key=lambda item: (item.snapshot_at, _maturity_rank(item.maturity)))
        return points


class TreasuryStore:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._open_connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS treasury_observations (
                    snapshot_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    label TEXT NOT NULL,
                    maturity TEXT NOT NULL,
                    yield_pct REAL,
                    price REAL,
                    change_value REAL,
                    source_url TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (snapshot_at, symbol)
                );
                CREATE INDEX IF NOT EXISTS idx_treasury_snapshot
                ON treasury_observations (snapshot_at DESC);

                CREATE TABLE IF NOT EXISTS treasury_ingestion_runs (
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

    def write_observations(self, observations: Iterable[TreasuryPoint]) -> int:
        self.initialize()
        values = list(observations)
        if not values:
            return 0
        fetched_at = _utcnow().isoformat()
        with self._open_connection() as connection:
            connection.executemany(
                """
                INSERT OR REPLACE INTO treasury_observations (
                    snapshot_at, symbol, label, maturity, yield_pct, price, change_value,
                    source_url, provider, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.snapshot_at,
                        item.symbol,
                        item.label,
                        item.maturity,
                        item.yield_pct,
                        item.price,
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
                INSERT OR REPLACE INTO treasury_ingestion_runs (
                    run_id, started_at, completed_at, status, observation_count, snapshot_at, source_url, error_message
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
                    SELECT
                        symbol,
                        label,
                        maturity,
                        snapshot_at,
                        yield_pct,
                        price,
                        change_value,
                        source_url,
                        provider,
                        ROW_NUMBER() OVER (
                            PARTITION BY symbol
                            ORDER BY snapshot_at DESC
                        ) AS row_rank
                    FROM treasury_observations
                )
                SELECT symbol, label, maturity, snapshot_at, yield_pct, price, change_value, source_url, provider
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
            label = str(row["label"])
            maturity = str(row["maturity"])
            yield_pct = _coerce_float(row["yield_pct"])
            price = _coerce_float(row["price"])
            change_value = _coerce_float(row["change_value"])
            yield_str = f"{yield_pct:.3f}%" if yield_pct is not None else "n/a"
            price_str = f"{price:.3f}" if price is not None else "n/a"
            change_str = f"{change_value:+.3f}" if change_value is not None else "n/a"
            docs.append(
                Document(
                    source=f"treasury_{symbol}_{snapshot_at.replace(':', '').replace('-', '').replace('T', '_')}",
                    content=(
                        f"{label} ({symbol}, {maturity}) at {snapshot_at}: "
                        f"yield {yield_str}, price {price_str}, change {change_str}."
                    ),
                    kind="treasury_observation",
                    published_at=snapshot_at,
                    source_url=str(row["source_url"]),
                    metadata={
                        "series_id": symbol,
                        "series_label": label,
                        "category": "treasury",
                        "maturity": maturity,
                        "observation_date": snapshot_at,
                        "value": yield_pct,
                        "yield_pct": yield_pct,
                        "price": price,
                        "change_value": change_value,
                        "provider": str(row["provider"]),
                    },
                )
            )
        return docs

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    @contextlib.contextmanager
    def _open_connection(self) -> Iterable[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()


class TreasuryIngestionService:
    def __init__(self, *, store: TreasuryStore, client: TreasuryParYieldClient | None = None) -> None:
        self.store = store
        self.client = client or TreasuryParYieldClient()

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
            latest_snapshot = max(snapshots) if snapshots else None
            self.store.record_run(
                run_id=run_id,
                started_at=started_at,
                completed_at=_utcnow().isoformat(),
                status="completed",
                observation_count=observation_count,
                snapshot_at=latest_snapshot,
                source_url=source_url,
            )
            return {
                "run_id": run_id,
                "observation_count": observation_count,
                "snapshot_at": latest_snapshot,
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


def export_dashboard_treasury_js(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    output_js_path: str | Path,
    per_symbol_points: int = 8,
    raw_row_limit: int = 64,
) -> dict[str, object] | None:
    dashboard = build_treasury_dashboard_payload(db_path=db_path, row_limit=raw_row_limit)
    if dashboard is None:
        payload = None
    else:
        kg_payload = build_treasury_knowledge_graph_payload(db_path=db_path, per_symbol_limit=per_symbol_points)
        payload = {**dashboard, "treasury_graph": kg_payload["treasury_graph"] if kg_payload else None}
    output_path = Path(output_js_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if payload is None:
        output_path.write_text("window.__TREASURY_DASHBOARD_DATA__ = null;\n", encoding="utf-8")
        return None
    output_path.write_text(
        "window.__TREASURY_DASHBOARD_DATA__ = " + json.dumps(payload, sort_keys=True, indent=2) + ";\n",
        encoding="utf-8",
    )
    return payload


def build_treasury_dashboard_payload(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    row_limit: int = 64,
) -> dict[str, object] | None:
    row_limit = max(1, row_limit)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        latest_row = connection.execute(
            """
            SELECT snapshot_at
            FROM treasury_observations
            ORDER BY snapshot_at DESC
            LIMIT 1
            """
        ).fetchone()
        if latest_row is None:
            return None
        latest_snapshot_at = str(latest_row["snapshot_at"])
        rows = connection.execute(
            """
            SELECT snapshot_at, symbol, label, maturity, yield_pct, price, change_value, source_url, provider
            FROM treasury_observations
            WHERE snapshot_at = ?
            ORDER BY maturity ASC, symbol ASC
            LIMIT ?
            """,
            (latest_snapshot_at, row_limit),
        ).fetchall()
    finally:
        connection.close()

    points = [
        {
            "snapshot_at": str(row["snapshot_at"]),
            "symbol": str(row["symbol"]),
            "label": str(row["label"]),
            "maturity": str(row["maturity"]),
            "yield_pct": _coerce_float(row["yield_pct"]),
            "price": _coerce_float(row["price"]),
            "change_value": _coerce_float(row["change_value"]),
            "source_url": str(row["source_url"]),
            "provider": str(row["provider"]),
        }
        for row in rows
    ]
    points.sort(key=lambda item: _maturity_rank(str(item["maturity"])))
    source_url = str(points[0]["source_url"]) if points else DEFAULT_TREASURY_URL
    slope_history = build_treasury_slope_history(db_path=db_path)
    return {
        "generated_at": _utcnow().isoformat(),
        "latest_snapshot_at": latest_snapshot_at,
        "source_url": source_url,
        "provider": PROVIDER,
        "points": points,
        "slope_history": slope_history,
    }


def treasury_slope_bias(slope_pct: float) -> float:
    """Map the 10Y-2Y spread (in percentage points) onto the shared [-1, +1] bias scale.

    A flat/inverted curve means the market expects policy to stay tight or tighten
    further (positive bias); a steep curve implies easing ahead (negative bias).
    A 1.0pp spread is treated as the neutral anchor and 1.0pp of deviation saturates.
    """
    return round(max(-1.0, min(1.0, (1.0 - slope_pct) / 1.0)), 4)


def build_treasury_slope_history(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    long_symbol: str = "UST10Y",
    short_symbol: str = "UST2Y",
    limit: int = 400,
) -> list[dict[str, object]]:
    """Daily 10Y-2Y spread converted to bias units, oldest first.

    Experiment 7 needs a volatility estimate in the same units as the model's bias
    inputs; deriving it from raw yield levels understates real policy-signal moves.
    """
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT snapshot_at, symbol, yield_pct
            FROM treasury_observations
            WHERE symbol IN (?, ?) AND yield_pct IS NOT NULL
            ORDER BY snapshot_at ASC
            """,
            (long_symbol, short_symbol),
        ).fetchall()
    except sqlite3.DatabaseError:
        return []
    finally:
        connection.close()

    by_date: dict[str, dict[str, float]] = {}
    for row in rows:
        value = _coerce_float(row["yield_pct"])
        if value is None:
            continue
        by_date.setdefault(str(row["snapshot_at"]), {})[str(row["symbol"])] = value

    history: list[dict[str, object]] = []
    for snapshot_at in sorted(by_date):
        pair = by_date[snapshot_at]
        if long_symbol not in pair or short_symbol not in pair:
            continue
        slope = round(pair[long_symbol] - pair[short_symbol], 4)
        history.append(
            {
                "date": snapshot_at,
                "slope": slope,
                "long_yield": pair[long_symbol],
                "short_yield": pair[short_symbol],
                "bias": treasury_slope_bias(slope),
            }
        )
    return history[-max(1, limit) :]


def build_treasury_knowledge_graph_payload(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    per_symbol_limit: int = 8,
) -> dict[str, object] | None:
    per_symbol_limit = max(1, per_symbol_limit)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            WITH ranked AS (
                SELECT
                    snapshot_at, symbol, label, maturity, yield_pct, price, change_value,
                    ROW_NUMBER() OVER (
                        PARTITION BY symbol
                        ORDER BY snapshot_at DESC
                    ) AS row_rank
                FROM treasury_observations
            )
            SELECT snapshot_at, symbol, label, maturity, yield_pct, price, change_value
            FROM ranked
            WHERE row_rank <= ?
            ORDER BY symbol ASC, snapshot_at ASC
            """,
            (per_symbol_limit,),
        ).fetchall()
    finally:
        connection.close()
    if not rows:
        return None

    by_symbol: dict[str, list[sqlite3.Row]] = {}
    label_by_symbol: dict[str, str] = {}
    maturity_by_symbol: dict[str, str] = {}
    for row in rows:
        symbol = str(row["symbol"])
        by_symbol.setdefault(symbol, []).append(row)
        label_by_symbol[symbol] = str(row["label"])
        maturity_by_symbol[symbol] = str(row["maturity"])

    symbols = sorted(by_symbol.keys(), key=lambda item: _maturity_rank(maturity_by_symbol.get(item, item)))
    nodes: list[dict[str, object]] = []
    edges: list[dict[str, object]] = []
    x_base = 6.8
    z_span = max(1.0, (len(symbols) - 1) * 0.9)
    z_start = -z_span / 2
    obs_count = 0
    for symbol_index, symbol in enumerate(symbols):
        series_id = f"treasury-series:{symbol}"
        series_z = z_start + symbol_index * 0.9
        nodes.append(
            {
                "id": series_id,
                "kind": "treasury_series",
                "label": label_by_symbol.get(symbol, symbol),
                "sublabel": maturity_by_symbol.get(symbol, ""),
                "x": round(x_base, 3),
                "y": 1.2,
                "z": round(series_z, 3),
                "size": 0.15,
                "color": "#f2cc60",
            }
        )
        edges.append(
            {
                "source": "treasury-hub",
                "target": series_id,
                "color": "#f2cc60",
                "opacity": 0.18,
            }
        )

        previous_obs_id: str | None = None
        series_rows = by_symbol[symbol]
        min_yield = min([_coerce_float(item["yield_pct"]) or 0.0 for item in series_rows], default=0.0)
        max_yield = max([_coerce_float(item["yield_pct"]) or 0.0 for item in series_rows], default=1.0)
        span = max(0.0001, max_yield - min_yield)
        for idx, row in enumerate(series_rows):
            obs_count += 1
            obs_id = f"treasury-obs:{symbol}:{str(row['snapshot_at']).replace(':', '')}"
            yield_pct = _coerce_float(row["yield_pct"])
            obs_x = x_base + 1.3 + (idx / max(1, len(series_rows) - 1)) * 2.1
            obs_y = -1.6 + (((yield_pct or min_yield) - min_yield) / span) * 2.6
            obs_z = series_z + (0.05 if idx % 2 == 0 else -0.05)
            nodes.append(
                {
                    "id": obs_id,
                    "kind": "treasury_observation",
                    "label": str(row["snapshot_at"]),
                    "sublabel": f"{yield_pct:.3f}%" if yield_pct is not None else "n/a",
                    "x": round(obs_x, 3),
                    "y": round(obs_y, 3),
                    "z": round(obs_z, 3),
                    "size": 0.055 if idx < len(series_rows) - 1 else 0.085,
                    "color": "#f2cc60",
                }
            )
            edges.append(
                {
                    "source": series_id,
                    "target": obs_id,
                    "color": "#f2cc60",
                    "opacity": 0.1,
                }
            )
            if previous_obs_id is not None:
                edges.append(
                    {
                        "source": previous_obs_id,
                        "target": obs_id,
                        "color": "#f2cc60",
                        "opacity": 0.22,
                    }
                )
            previous_obs_id = obs_id

    nodes.append(
        {
            "id": "treasury-hub",
            "kind": "treasury_hub",
            "label": "US Treasuries",
            "sublabel": PROVIDER,
            "x": 5.1,
            "y": 2.9,
            "z": 0.0,
            "size": 0.24,
            "color": "#f2cc60",
        }
    )
    edges.append(
        {
            "source": "treasury-hub",
            "target": "fomc-hub",
            "color": "#f2cc60",
            "opacity": 0.3,
        }
    )
    return {
        "generated_at": _utcnow().isoformat(),
        "treasury_graph": {
            "nodes": nodes,
            "edges": edges,
            "stats": {
                "series_count": len(symbols),
                "observation_count": obs_count,
                "edge_count": len(edges),
            },
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest the U.S. Treasury daily par yield curve into SQLite."
    )
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="SQLite database path.")
    parser.add_argument(
        "--year",
        type=int,
        action="append",
        dest="years",
        help="Calendar year to ingest. Repeat to backfill multiple years (default: current year).",
    )
    parser.add_argument(
        "--start-year",
        type=int,
        help="Optional start year for a contiguous backfill range (used with --end-year).",
    )
    parser.add_argument(
        "--end-year",
        type=int,
        help="Optional end year for a contiguous backfill range (defaults to the current year).",
    )
    parser.add_argument(
        "--dashboard-js",
        help="Optional output path for window.__TREASURY_DASHBOARD_DATA__ payload.",
    )
    parser.add_argument(
        "--kg-points-per-symbol",
        type=int,
        default=8,
        help="Number of most-recent points per symbol to include in the Treasury graph payload.",
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

    service = TreasuryIngestionService(store=TreasuryStore(args.db_path))
    result = service.ingest(years=sorted(years))
    if args.dashboard_js:
        export_dashboard_treasury_js(
            db_path=args.db_path,
            output_js_path=args.dashboard_js,
            per_symbol_points=args.kg_points_per_symbol,
            raw_row_limit=args.raw_row_limit,
        )

    print(
        f"Ingested {result['observation_count']} Treasury points across {result['years']} "
        f"(latest snapshot_at={result['snapshot_at']}, source={result['source_url']})."
    )
    return 0


def _normalize_date(value: object) -> str | None:
    """Treasury publishes MM/DD/YYYY; normalise to ISO so string ordering is chronological."""
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _maturity_rank(maturity: str) -> float:
    months = MATURITY_MONTHS.get(maturity)
    if months is not None:
        return months
    text = maturity.strip().upper()
    suffix = text[-1:]
    try:
        count = float(text[:-1])
    except ValueError:
        return 9999.0
    return count if suffix == "M" else count * 12.0


def _coerce_float(value: object) -> float | None:
    if value in (None, "", "-"):
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
            "Accept-Language": "en-US,en;q=0.9",
        },
        method="GET",
    )
    with urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8", errors="replace")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


if __name__ == "__main__":
    raise SystemExit(main())
