from __future__ import annotations

import argparse
import contextlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping
from urllib.request import Request, urlopen

from .rag import Document


DEFAULT_DB_PATH = Path("data") / "market_snapshots.db"
BLS_TIMESERIES_API_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"


@dataclass(frozen=True)
class CPISeriesDefinition:
    series_id: str
    label: str
    category: str
    seasonal_adjustment: str
    display_order: int
    color: str


@dataclass(frozen=True)
class CPIObservation:
    series_id: str
    observation_date: str
    year: int
    period: str
    period_name: str
    value: float
    footnotes: tuple[str, ...] = ()


JsonPoster = Callable[[str, Mapping[str, object]], Mapping[str, object] | list[object]]


class CPIIngestionError(RuntimeError):
    pass


CPI_SERIES_DEFINITIONS: tuple[CPISeriesDefinition, ...] = (
    CPISeriesDefinition("CUSR0000SA0", "CPI All Items (SA)", "headline", "SA", 1, "#ff7b72"),
    CPISeriesDefinition("CUSR0000SA0L1E", "CPI Core ex Food & Energy (SA)", "core", "SA", 2, "#ffa657"),
    CPISeriesDefinition("CUSR0000SAF11", "CPI Food at Home (SA)", "food", "SA", 3, "#8dc8ff"),
    CPISeriesDefinition("CUSR0000SAH1", "CPI Shelter (SA)", "housing", "SA", 4, "#79c0ff"),
    CPISeriesDefinition("CUSR0000SEHF", "CPI Energy (SA)", "energy", "SA", 5, "#58a6ff"),
    CPISeriesDefinition("CUSR0000SEHF01", "CPI Gasoline (SA)", "energy", "SA", 6, "#4ecdc4"),
    CPISeriesDefinition("CUSR0000SEHA", "CPI Rent of Shelter (SA)", "housing", "SA", 7, "#7bd389"),
    CPISeriesDefinition("CUSR0000SASLE", "CPI Services less Energy (SA)", "services", "SA", 8, "#66c7f4"),
    CPISeriesDefinition("CUSR0000SACL1E", "CPI Commodities less Food & Energy (SA)", "goods", "SA", 9, "#d2a8ff"),
)


class BLSCPIClient:
    def __init__(
        self,
        post_json: JsonPoster | None = None,
        api_url: str = BLS_TIMESERIES_API_URL,
    ) -> None:
        self.post_json = post_json or _post_json
        self.api_url = api_url

    def fetch_observations(
        self,
        *,
        series_ids: Iterable[str],
        start_year: int,
        end_year: int,
    ) -> dict[str, list[CPIObservation]]:
        payload = {
            "seriesid": list(series_ids),
            "startyear": str(start_year),
            "endyear": str(end_year),
        }
        response = self.post_json(self.api_url, payload)
        if not isinstance(response, Mapping):
            raise CPIIngestionError("BLS endpoint returned a non-object payload.")
        if str(response.get("status", "")).upper() != "REQUEST_SUCCEEDED":
            message = response.get("message")
            raise CPIIngestionError(f"BLS request failed: {message}")

        results = response.get("Results")
        if not isinstance(results, Mapping):
            raise CPIIngestionError("BLS payload did not include Results.")
        raw_series = results.get("series")
        if not isinstance(raw_series, list):
            raise CPIIngestionError("BLS payload did not include series data.")

        observations_by_series: dict[str, list[CPIObservation]] = {}
        for raw_series_item in raw_series:
            if not isinstance(raw_series_item, Mapping):
                continue
            series_id = str(raw_series_item.get("seriesID") or "").strip()
            if not series_id:
                continue
            series_observations: list[CPIObservation] = []
            raw_points = raw_series_item.get("data")
            if not isinstance(raw_points, list):
                continue
            for raw_point in raw_points:
                if not isinstance(raw_point, Mapping):
                    continue
                period = str(raw_point.get("period") or "")
                if not period.startswith("M") or len(period) != 3:
                    continue
                value = _coerce_float(raw_point.get("value"))
                if value is None:
                    continue
                year = int(str(raw_point.get("year") or "0"))
                month = int(period[1:])
                obs_date = f"{year:04d}-{month:02d}-01"
                series_observations.append(
                    CPIObservation(
                        series_id=series_id,
                        observation_date=obs_date,
                        year=year,
                        period=period,
                        period_name=str(raw_point.get("periodName") or period),
                        value=value,
                        footnotes=tuple(_extract_footnotes(raw_point.get("footnotes"))),
                    )
                )
            series_observations.sort(key=lambda item: item.observation_date)
            observations_by_series[series_id] = series_observations
        return observations_by_series


class CPIStore:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._open_connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cpi_series_catalog (
                    series_id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    category TEXT NOT NULL,
                    seasonal_adjustment TEXT NOT NULL,
                    display_order INTEGER NOT NULL,
                    color TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    metadata_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cpi_observations (
                    series_id TEXT NOT NULL,
                    observation_date TEXT NOT NULL,
                    year INTEGER NOT NULL,
                    period TEXT NOT NULL,
                    period_name TEXT NOT NULL,
                    value REAL NOT NULL,
                    footnotes_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (series_id, observation_date)
                );
                CREATE INDEX IF NOT EXISTS idx_cpi_observations_date
                ON cpi_observations (observation_date DESC);

                CREATE TABLE IF NOT EXISTS cpi_metrics (
                    metric_date TEXT NOT NULL,
                    metric_key TEXT NOT NULL,
                    metric_value REAL NOT NULL,
                    metadata_json TEXT NOT NULL,
                    computed_at TEXT NOT NULL,
                    PRIMARY KEY (metric_date, metric_key)
                );

                CREATE TABLE IF NOT EXISTS cpi_ingestion_runs (
                    run_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    observation_count INTEGER,
                    metric_date TEXT,
                    error_message TEXT
                );
                """
            )

    def write_series_catalog(self, series_definitions: Iterable[CPISeriesDefinition]) -> None:
        self.initialize()
        updated_at = _utcnow().isoformat()
        with self._open_connection() as connection:
            connection.executemany(
                """
                INSERT OR REPLACE INTO cpi_series_catalog (
                    series_id, label, category, seasonal_adjustment,
                    display_order, color, active, metadata_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.series_id,
                        item.label,
                        item.category,
                        item.seasonal_adjustment,
                        item.display_order,
                        item.color,
                        1,
                        json.dumps({}, sort_keys=True),
                        updated_at,
                    )
                    for item in series_definitions
                ],
            )

    def write_observations(self, observations: Iterable[CPIObservation]) -> int:
        self.initialize()
        fetched_at = _utcnow().isoformat()
        values = list(observations)
        if not values:
            return 0
        with self._open_connection() as connection:
            connection.executemany(
                """
                INSERT OR REPLACE INTO cpi_observations (
                    series_id, observation_date, year, period, period_name, value, footnotes_json, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.series_id,
                        item.observation_date,
                        item.year,
                        item.period,
                        item.period_name,
                        item.value,
                        json.dumps(list(item.footnotes), sort_keys=True),
                        fetched_at,
                    )
                    for item in values
                ],
            )
        return len(values)

    def write_metrics(
        self,
        *,
        metric_date: str,
        metrics: Mapping[str, float],
        metadata_by_key: Mapping[str, Mapping[str, object]],
    ) -> None:
        self.initialize()
        computed_at = _utcnow().isoformat()
        with self._open_connection() as connection:
            connection.executemany(
                """
                INSERT OR REPLACE INTO cpi_metrics (
                    metric_date, metric_key, metric_value, metadata_json, computed_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        metric_date,
                        key,
                        float(value),
                        json.dumps(dict(metadata_by_key.get(key, {})), sort_keys=True),
                        computed_at,
                    )
                    for key, value in metrics.items()
                ],
            )

    def record_run(
        self,
        *,
        run_id: str,
        started_at: str,
        status: str,
        completed_at: str | None = None,
        observation_count: int | None = None,
        metric_date: str | None = None,
        error_message: str | None = None,
    ) -> None:
        self.initialize()
        with self._open_connection() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO cpi_ingestion_runs (
                    run_id, started_at, completed_at, status, observation_count, metric_date, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, started_at, completed_at, status, observation_count, metric_date, error_message),
            )

    def load_documents(self, *, per_series_limit: int = 24) -> list[Document]:
        self.initialize()
        with self._open_connection() as connection:
            rows = connection.execute(
                """
                WITH ranked AS (
                    SELECT
                        o.series_id,
                        c.label,
                        c.category,
                        o.observation_date,
                        o.value,
                        ROW_NUMBER() OVER (
                            PARTITION BY o.series_id
                            ORDER BY o.observation_date DESC
                        ) AS row_rank
                    FROM cpi_observations o
                    JOIN cpi_series_catalog c ON c.series_id = o.series_id
                )
                SELECT series_id, label, category, observation_date, value
                FROM ranked
                WHERE row_rank <= ?
                ORDER BY series_id ASC, observation_date DESC
                """,
                (per_series_limit,),
            ).fetchall()

        docs: list[Document] = []
        for row in rows:
            observation_date = str(row["observation_date"])
            series_id = str(row["series_id"])
            label = str(row["label"])
            value = float(row["value"])
            docs.append(
                Document(
                    source=f"cpi_{series_id}_{observation_date.replace('-', '')}",
                    content=(
                        f"CPI observation for {label} ({series_id}) on {observation_date}: "
                        f"index value {value:.3f}."
                    ),
                    kind="cpi_observation",
                    published_at=observation_date,
                    source_url="https://api.bls.gov/publicAPI/v2/timeseries/data/",
                    metadata={
                        "series_id": series_id,
                        "series_label": label,
                        "category": str(row["category"]),
                        "observation_date": observation_date,
                        "value": value,
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


class CPIIngestionService:
    def __init__(
        self,
        store: CPIStore,
        client: BLSCPIClient | None = None,
        series_definitions: Iterable[CPISeriesDefinition] = CPI_SERIES_DEFINITIONS,
    ) -> None:
        self.store = store
        self.client = client or BLSCPIClient()
        self.series_definitions = tuple(series_definitions)

    def ingest(self, *, start_year: int, end_year: int) -> dict[str, object]:
        if start_year > end_year:
            raise CPIIngestionError("start_year cannot be greater than end_year.")

        run_id = str(uuid.uuid4())
        started_at = _utcnow().isoformat()
        self.store.record_run(run_id=run_id, started_at=started_at, status="started")
        try:
            self.store.write_series_catalog(self.series_definitions)
            observations_by_series = self.client.fetch_observations(
                series_ids=[item.series_id for item in self.series_definitions],
                start_year=start_year,
                end_year=end_year,
            )
            all_observations = [obs for values in observations_by_series.values() for obs in values]
            observation_count = self.store.write_observations(all_observations)

            metric_date, metrics, metadata_by_key = _compute_latest_cpi_metrics(observations_by_series)
            if metric_date:
                self.store.write_metrics(
                    metric_date=metric_date,
                    metrics=metrics,
                    metadata_by_key=metadata_by_key,
                )

            completed_at = _utcnow().isoformat()
            self.store.record_run(
                run_id=run_id,
                started_at=started_at,
                completed_at=completed_at,
                status="completed",
                observation_count=observation_count,
                metric_date=metric_date,
            )
            return {
                "run_id": run_id,
                "observation_count": observation_count,
                "metric_date": metric_date,
                "series_count": len(observations_by_series),
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


def export_dashboard_cpi_js(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    output_js_path: str | Path,
) -> dict[str, object] | None:
    payload = build_dashboard_cpi_payload(db_path=db_path)
    output_path = Path(output_js_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if payload is None:
        output_path.write_text("window.__CPI_DASHBOARD_DATA__ = null;\n", encoding="utf-8")
        return None
    output_path.write_text(
        "window.__CPI_DASHBOARD_DATA__ = " + json.dumps(payload, sort_keys=True, indent=2) + ";\n",
        encoding="utf-8",
    )
    return payload


def export_dashboard_kg_js(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    output_js_path: str | Path,
    months: int = 24,
) -> dict[str, object] | None:
    payload = build_cpi_knowledge_graph_payload(db_path=db_path, months=months)
    output_path = Path(output_js_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if payload is None:
        output_path.write_text("window.__KG_DASHBOARD_DATA__ = null;\n", encoding="utf-8")
        return None
    output_path.write_text(
        "window.__KG_DASHBOARD_DATA__ = " + json.dumps(payload, sort_keys=True, indent=2) + ";\n",
        encoding="utf-8",
    )
    return payload


def build_dashboard_cpi_payload(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, object] | None:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        metric_date_row = connection.execute(
            """
            SELECT metric_date
            FROM cpi_metrics
            WHERE metric_key = 'cpi_bias'
            ORDER BY metric_date DESC
            LIMIT 1
            """
        ).fetchone()
        if metric_date_row is None:
            return None
        metric_date = str(metric_date_row["metric_date"])
        metric_rows = connection.execute(
            """
            SELECT metric_key, metric_value, metadata_json
            FROM cpi_metrics
            WHERE metric_date = ?
            """,
            (metric_date,),
        ).fetchall()
        value_rows = connection.execute(
            """
            SELECT o.series_id, o.value, c.label, c.category
            FROM cpi_observations o
            JOIN cpi_series_catalog c ON c.series_id = o.series_id
            WHERE o.observation_date = ?
            ORDER BY c.display_order ASC
            """,
            (metric_date,),
        ).fetchall()
    finally:
        connection.close()

    metrics = {
        str(row["metric_key"]): round(float(row["metric_value"]), 6)
        for row in metric_rows
    }
    metadata = {
        str(row["metric_key"]): json.loads(row["metadata_json"] or "{}")
        for row in metric_rows
    }
    heat_card = _build_cpi_heat_card(metric_date=metric_date, metrics=metrics)

    latest_values = [
        {
            "series_id": str(row["series_id"]),
            "label": str(row["label"]),
            "category": str(row["category"]),
            "value": round(float(row["value"]), 3),
        }
        for row in value_rows
    ]

    return {
        "generated_at": _utcnow().isoformat(),
        "metric_date": metric_date,
        "metrics": metrics,
        "metric_metadata": metadata,
        "heat_card": heat_card,
        "latest_values": latest_values,
        "source_url": "https://api.bls.gov/publicAPI/v2/timeseries/data/",
    }


def build_cpi_knowledge_graph_payload(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    months: int = 24,
) -> dict[str, object] | None:
    months = max(1, months)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        series_rows = connection.execute(
            """
            SELECT series_id, label, category, color, display_order
            FROM cpi_series_catalog
            WHERE active = 1
            ORDER BY display_order ASC
            """
        ).fetchall()
        if not series_rows:
            return None

        obs_rows = connection.execute(
            """
            WITH ranked AS (
                SELECT
                    o.series_id,
                    o.observation_date,
                    o.value,
                    ROW_NUMBER() OVER (
                        PARTITION BY o.series_id
                        ORDER BY o.observation_date DESC
                    ) AS row_rank
                FROM cpi_observations o
            )
            SELECT series_id, observation_date, value
            FROM ranked
            WHERE row_rank <= ?
            ORDER BY series_id ASC, observation_date ASC
            """,
            (months,),
        ).fetchall()
        if not obs_rows:
            return None

        metric_date_row = connection.execute(
            """
            SELECT metric_date
            FROM cpi_metrics
            WHERE metric_key='cpi_bias'
            ORDER BY metric_date DESC
            LIMIT 1
            """
        ).fetchone()
        metric_rows: list[sqlite3.Row] = []
        metric_date: str | None = None
        if metric_date_row is not None:
            metric_date = str(metric_date_row["metric_date"])
            metric_rows = connection.execute(
                """
                SELECT metric_key, metric_value
                FROM cpi_metrics
                WHERE metric_date = ?
                ORDER BY metric_key
                """,
                (metric_date,),
            ).fetchall()
    finally:
        connection.close()

    series_lookup = {
        str(row["series_id"]): {
            "label": str(row["label"]),
            "category": str(row["category"]),
            "color": str(row["color"]),
            "display_order": int(row["display_order"]),
        }
        for row in series_rows
    }
    by_series: dict[str, list[dict[str, object]]] = {}
    for row in obs_rows:
        series_id = str(row["series_id"])
        by_series.setdefault(series_id, []).append(
            {"observation_date": str(row["observation_date"]), "value": float(row["value"])}
        )

    nodes: list[dict[str, object]] = []
    edges: list[dict[str, object]] = []
    position_map: dict[str, tuple[float, float, float]] = {"fomc-hub": (0.0, 1.0, 0.0)}
    observation_count = 0

    series_ids = [str(row["series_id"]) for row in series_rows]
    z_span = max(1.0, (len(series_ids) - 1) * 1.2)
    z_start = -z_span / 2
    cluster_x = 7.0
    for series_idx, series_id in enumerate(series_ids):
        info = series_lookup[series_id]
        series_node_id = f"cpi-series:{series_id}"
        series_z = z_start + series_idx * 1.2
        series_pos = (cluster_x, 1.1, series_z)
        position_map[series_node_id] = series_pos
        nodes.append(
            {
                "id": series_node_id,
                "kind": "cpi_series",
                "label": info["label"],
                "sublabel": series_id,
                "x": round(series_pos[0], 3),
                "y": round(series_pos[1], 3),
                "z": round(series_pos[2], 3),
                "size": 0.16,
                "color": info["color"],
            }
        )

        observations = by_series.get(series_id, [])
        if not observations:
            continue
        min_value = min(float(item["value"]) for item in observations)
        max_value = max(float(item["value"]) for item in observations)
        value_range = max(0.0001, max_value - min_value)
        last_obs_node_id: str | None = None
        for index, item in enumerate(observations):
            observation_count += 1
            obs_id = f"cpi-obs:{series_id}:{item['observation_date']}"
            x = cluster_x - 3.2 + (index / max(1, len(observations) - 1)) * 6.4
            y = -2.3 + ((float(item["value"]) - min_value) / value_range) * 2.8
            z = series_z + (0.06 if index % 2 == 0 else -0.06)
            position_map[obs_id] = (x, y, z)
            nodes.append(
                {
                    "id": obs_id,
                    "kind": "cpi_observation",
                    "label": str(item["observation_date"]),
                    "sublabel": f"{float(item['value']):.1f}",
                    "x": round(x, 3),
                    "y": round(y, 3),
                    "z": round(z, 3),
                    "size": 0.055 if index < len(observations) - 1 else 0.085,
                    "color": info["color"],
                }
            )
            if last_obs_node_id is not None:
                edges.append(
                    {
                        "source": last_obs_node_id,
                        "target": obs_id,
                        "color": info["color"],
                        "opacity": 0.2,
                    }
                )
            edges.append(
                {
                    "source": series_node_id,
                    "target": obs_id,
                    "color": info["color"],
                    "opacity": 0.08,
                }
            )
            last_obs_node_id = obs_id

    heat_card: dict[str, object] | None = None
    if metric_date is not None and metric_rows:
        metrics = {str(row["metric_key"]): float(row["metric_value"]) for row in metric_rows}
        heat_card = _build_cpi_heat_card(metric_date=metric_date, metrics=metrics)
        metric_anchor = ("cpi-metric-anchor", 4.8, 2.9, 0.0)
        nodes.append(
            {
                "id": metric_anchor[0],
                "kind": "cpi_metric",
                "label": "CPI Composite",
                "sublabel": str(heat_card["display"]),
                "x": metric_anchor[1],
                "y": metric_anchor[2],
                "z": metric_anchor[3],
                "size": 0.22,
                "color": _tone_hex(str(heat_card["tone"])),
            }
        )
        position_map[metric_anchor[0]] = (metric_anchor[1], metric_anchor[2], metric_anchor[3])
        edges.append(
            {
                "source": metric_anchor[0],
                "target": "fomc-hub",
                "color": "#8dc8ff",
                "opacity": 0.28,
            }
        )
        for idx, row in enumerate(metric_rows):
            metric_id = f"cpi-metric:{str(row['metric_key'])}"
            metric_x = 3.8 + idx * 0.48
            metric_y = 3.7
            metric_z = -1.2 + idx * 0.35
            nodes.append(
                {
                    "id": metric_id,
                    "kind": "cpi_metric",
                    "label": str(row["metric_key"]),
                    "sublabel": f"{float(row['metric_value']):.3f}",
                    "x": round(metric_x, 3),
                    "y": round(metric_y, 3),
                    "z": round(metric_z, 3),
                    "size": 0.08,
                    "color": "#8dc8ff",
                }
            )
            position_map[metric_id] = (metric_x, metric_y, metric_z)
            edges.append(
                {
                    "source": metric_anchor[0],
                    "target": metric_id,
                    "color": "#8dc8ff",
                    "opacity": 0.24,
                }
            )

    return {
        "generated_at": _utcnow().isoformat(),
        "metric_date": metric_date,
        "cpi_heat_card": heat_card,
        "cpi_graph": {
            "nodes": nodes,
            "edges": edges,
            "stats": {
                "series_count": len(series_ids),
                "observation_count": observation_count,
                "edge_count": len(edges),
            },
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest CPI time series data from BLS into SQLite.")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="SQLite database path.")
    parser.add_argument("--start-year", type=int, default=date.today().year - 2, help="First year to ingest.")
    parser.add_argument("--end-year", type=int, default=date.today().year, help="Last year to ingest.")
    parser.add_argument(
        "--dashboard-js",
        help="Optional output path for window.__CPI_DASHBOARD_DATA__ payload.",
    )
    parser.add_argument(
        "--kg-js",
        help="Optional output path for window.__KG_DASHBOARD_DATA__ payload with CPI nodes/edges.",
    )
    parser.add_argument(
        "--kg-months",
        type=int,
        default=24,
        help="Months of CPI points to include in KG export (default: 24).",
    )
    args = parser.parse_args(argv)

    store = CPIStore(args.db_path)
    service = CPIIngestionService(store=store)
    result = service.ingest(start_year=args.start_year, end_year=args.end_year)

    if args.dashboard_js:
        export_dashboard_cpi_js(db_path=args.db_path, output_js_path=args.dashboard_js)
    if args.kg_js:
        export_dashboard_kg_js(db_path=args.db_path, output_js_path=args.kg_js, months=args.kg_months)

    print(
        f"Ingested {result['observation_count']} CPI observations across {result['series_count']} series "
        f"(metric_date={result['metric_date']})."
    )
    return 0


def _compute_latest_cpi_metrics(
    observations_by_series: Mapping[str, list[CPIObservation]],
) -> tuple[str | None, dict[str, float], dict[str, Mapping[str, object]]]:
    headline_id = "CUSR0000SA0"
    core_id = "CUSR0000SA0L1E"
    headline = _observation_lookup(observations_by_series.get(headline_id, []))
    core = _observation_lookup(observations_by_series.get(core_id, []))
    if not headline or not core:
        return None, {}, {}

    common_dates = sorted(set(headline.keys()).intersection(core.keys()))
    if not common_dates:
        return None, {}, {}
    metric_date = common_dates[-1]

    headline_yoy = _pct_change(headline.get(metric_date), headline.get(_shift_month(metric_date, -12)))
    headline_mom = _pct_change(headline.get(metric_date), headline.get(_shift_month(metric_date, -1)))
    headline_3m_ann = _annualized_change(headline.get(metric_date), headline.get(_shift_month(metric_date, -3)), 3)

    core_yoy = _pct_change(core.get(metric_date), core.get(_shift_month(metric_date, -12)))
    core_mom = _pct_change(core.get(metric_date), core.get(_shift_month(metric_date, -1)))
    core_3m_ann = _annualized_change(core.get(metric_date), core.get(_shift_month(metric_date, -3)), 3)
    momentum_accel = None
    if core_3m_ann is not None and core_yoy is not None:
        momentum_accel = core_3m_ann - core_yoy

    cpi_bias = _compute_cpi_bias(
        headline_yoy=headline_yoy,
        core_yoy=core_yoy,
        core_3m_annualized=core_3m_ann,
    )
    heat_score = float(_score_from_bias(cpi_bias))

    metrics: dict[str, float] = {
        "cpi_bias": cpi_bias,
        "cpi_heat_score": heat_score,
    }
    if headline_yoy is not None:
        metrics["headline_yoy"] = headline_yoy
    if headline_mom is not None:
        metrics["headline_mom"] = headline_mom
    if headline_3m_ann is not None:
        metrics["headline_3m_annualized"] = headline_3m_ann
    if core_yoy is not None:
        metrics["core_yoy"] = core_yoy
    if core_mom is not None:
        metrics["core_mom"] = core_mom
    if core_3m_ann is not None:
        metrics["core_3m_annualized"] = core_3m_ann
    if momentum_accel is not None:
        metrics["core_momentum_accel"] = momentum_accel

    metadata_by_key: dict[str, Mapping[str, object]] = {
        "cpi_bias": {
            "formula": "clamp((0.5*core_yoy_gap + 0.3*core_3m_gap + 0.2*headline_yoy_gap) / 0.03, -1, 1)",
            "target_inflation": 0.02,
            "weights": {"core_yoy": 0.5, "core_3m_annualized": 0.3, "headline_yoy": 0.2},
        }
    }
    return metric_date, metrics, metadata_by_key


def _build_cpi_heat_card(*, metric_date: str, metrics: Mapping[str, float]) -> dict[str, object]:
    bias = float(metrics.get("cpi_bias", 0.0))
    score = _score_from_bias(bias)
    tone = _tone_from_score(score)
    sign = "+" if bias > 0 else ""
    display = f"{sign}{bias:.2f}"
    source_pills = [f"BLS CPI {metric_date}", f"bias {display}"]
    if "core_yoy" in metrics:
        source_pills.append(f"core YoY {metrics['core_yoy'] * 100:.2f}%")
    if "headline_yoy" in metrics:
        source_pills.append(f"headline YoY {metrics['headline_yoy'] * 100:.2f}%")
    if "core_3m_annualized" in metrics:
        source_pills.append(f"core 3m ann {metrics['core_3m_annualized'] * 100:.2f}%")
    return {
        "label": "CPI Momentum",
        "display": display,
        "score": score,
        "tone": tone,
        "toneLabel": "inflation",
        "sources": source_pills,
    }


def _compute_cpi_bias(
    *,
    headline_yoy: float | None,
    core_yoy: float | None,
    core_3m_annualized: float | None,
) -> float:
    target = 0.02
    core_yoy_gap = ((core_yoy or target) - target)
    core_3m_gap = ((core_3m_annualized or target) - target)
    headline_gap = ((headline_yoy or target) - target)
    raw = (0.5 * core_yoy_gap + 0.3 * core_3m_gap + 0.2 * headline_gap) / 0.03
    return round(max(-1.0, min(1.0, raw)), 4)


def _observation_lookup(observations: Iterable[CPIObservation]) -> dict[str, float]:
    return {item.observation_date: item.value for item in observations}


def _pct_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous in (None, 0.0):
        return None
    return round((current / previous) - 1.0, 6)


def _annualized_change(current: float | None, previous: float | None, months: int) -> float | None:
    if current is None or previous in (None, 0.0):
        return None
    if months <= 0:
        return None
    periods_per_year = 12 / months
    return round((current / previous) ** periods_per_year - 1.0, 6)


def _shift_month(iso_date: str, month_delta: int) -> str:
    year_str, month_str, _day_str = iso_date.split("-")
    year = int(year_str)
    month = int(month_str)
    absolute = (year * 12 + (month - 1)) + month_delta
    shifted_year = absolute // 12
    shifted_month = absolute % 12 + 1
    return f"{shifted_year:04d}-{shifted_month:02d}-01"


def _extract_footnotes(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    values: list[str] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        text = str(item.get("text") or "").strip()
        if text:
            values.append(text)
    return values


def _coerce_float(value: object) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
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


def _tone_hex(tone: str) -> str:
    mapping = {
        "hot": "#ff7b72",
        "warm": "#ffa657",
        "balanced": "#8dc8ff",
        "cool": "#79c0ff",
        "cold": "#58a6ff",
    }
    return mapping.get(tone, "#8dc8ff")


def _post_json(url: str, payload: Mapping[str, object]) -> Mapping[str, object] | list[object]:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "User-Agent": "WhatTheFed/1.0",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        body = json.loads(response.read().decode("utf-8"))
    if not isinstance(body, (dict, list)):
        raise CPIIngestionError(f"Expected JSON object or array from {url}.")
    return body


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


if __name__ == "__main__":
    raise SystemExit(main())
