from __future__ import annotations

import argparse
import contextlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping
from urllib.request import Request, urlopen

from .rag import Document


DEFAULT_DB_PATH = Path("data") / "market_snapshots.db"
DEFAULT_BLOOMBERG_URL = "https://www.bloomberg.com/markets/rates-bonds/government-bonds/us"
NEXT_DATA_RE = re.compile(
    r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>\s*(?P<payload>\{.*?\})\s*</script>',
    re.IGNORECASE | re.DOTALL,
)
MATURITY_RE = re.compile(r"(?P<count>\d+)\s*(?P<unit>year|yr|y|month|mo)s?\b", re.IGNORECASE)


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
    provider: str = "Bloomberg"


GetText = Callable[[str], str]


class TreasuryIngestionError(RuntimeError):
    pass


class BloombergTreasuryClient:
    def __init__(
        self,
        *,
        get_text: GetText | None = None,
        source_url: str = DEFAULT_BLOOMBERG_URL,
    ) -> None:
        self.get_text = get_text or _get_text
        self.source_url = source_url

    def fetch_points(self, *, input_json_path: str | Path | None = None) -> tuple[str, list[TreasuryPoint], str]:
        if input_json_path is not None:
            return self._load_points_from_json(Path(input_json_path))
        html = self.get_text(self.source_url)
        return self._parse_points_from_html(html, self.source_url)

    def _load_points_from_json(self, path: Path) -> tuple[str, list[TreasuryPoint], str]:
        if not path.exists():
            raise TreasuryIngestionError(f"Treasury input JSON not found: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise TreasuryIngestionError("Treasury input JSON must be an object.")

        source_url = str(payload.get("source_url") or self.source_url)
        snapshot_at = str(payload.get("snapshot_at") or payload.get("as_of") or _utcnow().isoformat())
        raw_points = payload.get("points")
        if not isinstance(raw_points, list):
            raise TreasuryIngestionError("Treasury input JSON must include a points array.")

        points: list[TreasuryPoint] = []
        for item in raw_points:
            if not isinstance(item, Mapping):
                continue
            symbol = str(item.get("symbol") or item.get("ticker") or "").strip()
            label = str(item.get("label") or item.get("name") or symbol).strip()
            maturity = str(item.get("maturity") or _infer_maturity(label) or symbol).strip()
            if not symbol or not maturity:
                continue
            points.append(
                TreasuryPoint(
                    snapshot_at=snapshot_at,
                    symbol=symbol,
                    label=label or symbol,
                    maturity=maturity,
                    yield_pct=_coerce_float(item.get("yield_pct") or item.get("yield")),
                    price=_coerce_float(item.get("price")),
                    change_value=_coerce_float(item.get("change_value") or item.get("change")),
                    source_url=source_url,
                )
            )

        if not points:
            raise TreasuryIngestionError("Treasury input JSON did not contain any valid points.")
        return snapshot_at, points, source_url

    def _parse_points_from_html(self, html: str, source_url: str) -> tuple[str, list[TreasuryPoint], str]:
        match = NEXT_DATA_RE.search(html)
        if match is None:
            raise TreasuryIngestionError(
                "Could not find Bloomberg page JSON payload. Use --input-json with a Bloomberg snapshot export."
            )
        data = json.loads(match.group("payload"))
        rows = _extract_candidate_rows(data)
        points: list[TreasuryPoint] = []
        snapshot_at = str(_find_first_value(data, ("asOf", "as_of", "timestamp", "time")) or _utcnow().isoformat())
        seen: set[tuple[str, str]] = set()
        for row in rows:
            symbol = str(
                row.get("symbol")
                or row.get("ticker")
                or row.get("security")
                or row.get("id")
                or row.get("code")
                or ""
            ).strip()
            label = str(row.get("label") or row.get("name") or row.get("securityName") or row.get("description") or symbol)
            maturity = str(row.get("maturity") or _infer_maturity(label) or _infer_maturity(symbol) or "").strip()
            yield_pct = _coerce_float(
                row.get("yield")
                or row.get("yieldPct")
                or row.get("yieldPercent")
                or row.get("yieldToMaturity")
                or row.get("lastYield")
            )
            price = _coerce_float(row.get("price") or row.get("lastPrice") or row.get("pxLast"))
            change_value = _coerce_float(
                row.get("change")
                or row.get("changeValue")
                or row.get("dailyChange")
                or row.get("netChange")
                or row.get("priceChange")
            )
            if not symbol or not maturity or yield_pct is None:
                continue
            dedupe_key = (symbol, maturity)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            points.append(
                TreasuryPoint(
                    snapshot_at=snapshot_at,
                    symbol=symbol,
                    label=label.strip() or symbol,
                    maturity=maturity,
                    yield_pct=yield_pct,
                    price=price,
                    change_value=change_value,
                    source_url=source_url,
                )
            )

        if not points:
            raise TreasuryIngestionError(
                "Bloomberg page parsed but no Treasury rows were found. Use --input-json with a captured snapshot."
            )
        points.sort(key=lambda item: _maturity_rank(item.maturity))
        return snapshot_at, points, source_url


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
    def __init__(self, *, store: TreasuryStore, client: BloombergTreasuryClient | None = None) -> None:
        self.store = store
        self.client = client or BloombergTreasuryClient()

    def ingest(self, *, input_json_path: str | Path | None = None) -> dict[str, object]:
        run_id = str(uuid.uuid4())
        started_at = _utcnow().isoformat()
        self.store.record_run(run_id=run_id, started_at=started_at, status="started")
        try:
            snapshot_at, points, source_url = self.client.fetch_points(input_json_path=input_json_path)
            observation_count = self.store.write_observations(points)
            self.store.record_run(
                run_id=run_id,
                started_at=started_at,
                completed_at=_utcnow().isoformat(),
                status="completed",
                observation_count=observation_count,
                snapshot_at=snapshot_at,
                source_url=source_url,
            )
            return {
                "run_id": run_id,
                "observation_count": observation_count,
                "snapshot_at": snapshot_at,
                "source_url": source_url,
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
    source_url = str(points[0]["source_url"]) if points else DEFAULT_BLOOMBERG_URL
    return {
        "generated_at": _utcnow().isoformat(),
        "latest_snapshot_at": latest_snapshot_at,
        "source_url": source_url,
        "provider": "Bloomberg",
        "points": points,
    }


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
            "sublabel": "Bloomberg",
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
    parser = argparse.ArgumentParser(description="Ingest Bloomberg US Treasury bond points into SQLite.")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="SQLite database path.")
    parser.add_argument(
        "--input-json",
        help=(
            "Optional path to a local Bloomberg snapshot JSON file "
            "(format: {snapshot_at, source_url, points:[{symbol,label,maturity,yield_pct,price,change_value}]})"
        ),
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

    service = TreasuryIngestionService(store=TreasuryStore(args.db_path))
    result = service.ingest(input_json_path=args.input_json)
    if args.dashboard_js:
        export_dashboard_treasury_js(
            db_path=args.db_path,
            output_js_path=args.dashboard_js,
            per_symbol_points=args.kg_points_per_symbol,
            raw_row_limit=args.raw_row_limit,
        )

    print(
        f"Ingested {result['observation_count']} Treasury points "
        f"(snapshot_at={result['snapshot_at']}, source={result['source_url']})."
    )
    return 0


def _extract_candidate_rows(value: object) -> list[Mapping[str, object]]:
    rows: list[Mapping[str, object]] = []
    if isinstance(value, Mapping):
        if any(key in value for key in ("symbol", "ticker", "securityName", "maturity")):
            rows.append(value)
        for child in value.values():
            rows.extend(_extract_candidate_rows(child))
    elif isinstance(value, list):
        for item in value:
            rows.extend(_extract_candidate_rows(item))
    return rows


def _find_first_value(value: object, keys: tuple[str, ...]) -> object | None:
    if isinstance(value, Mapping):
        for key in keys:
            if key in value:
                return value[key]
        for child in value.values():
            found = _find_first_value(child, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_first_value(item, keys)
            if found is not None:
                return found
    return None


def _infer_maturity(text: str) -> str | None:
    match = MATURITY_RE.search(text)
    if match is None:
        return None
    count = int(match.group("count"))
    unit = match.group("unit").lower()
    if unit.startswith("m"):
        return f"{count}M"
    return f"{count}Y"


def _maturity_rank(maturity: str) -> float:
    parsed = _infer_maturity(maturity)
    if parsed is None:
        return 9999.0
    count = int(parsed[:-1])
    if parsed.endswith("M"):
        return float(count)
    return float(count * 12)


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
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.bloomberg.com/",
        },
        method="GET",
    )
    with urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


if __name__ == "__main__":
    raise SystemExit(main())
