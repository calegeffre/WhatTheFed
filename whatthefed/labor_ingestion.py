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
class LaborSeriesDefinition:
    series_id: str
    label: str
    category: str
    seasonal_adjustment: str
    display_order: int
    color: str


@dataclass(frozen=True)
class LaborObservation:
    series_id: str
    observation_date: str
    year: int
    period: str
    period_name: str
    value: float
    footnotes: tuple[str, ...] = ()


JsonPoster = Callable[[str, Mapping[str, object]], Mapping[str, object] | list[object]]


class LaborIngestionError(RuntimeError):
    pass


LABOR_SERIES_DEFINITIONS: tuple[LaborSeriesDefinition, ...] = (
    LaborSeriesDefinition("LNS14000000", "Unemployment Rate (U-3)", "unemployment", "NSA", 1, "#ff7b72"),
    LaborSeriesDefinition("LNS11300000", "Labor Force Participation Rate", "participation", "NSA", 2, "#ffa657"),
    LaborSeriesDefinition(
        "LNS12300060", "Prime-Age Employment-Population Ratio", "participation", "NSA", 3, "#8dc8ff"
    ),
    LaborSeriesDefinition("CES0000000001", "Total Nonfarm Payrolls", "payrolls", "SA", 4, "#79c0ff"),
    LaborSeriesDefinition("CES0500000003", "Average Hourly Earnings (Private)", "wages", "SA", 5, "#58a6ff"),
    LaborSeriesDefinition("LNS13000000", "Unemployed Level", "unemployment", "NSA", 6, "#d2a8ff"),
    LaborSeriesDefinition("JTS000000000000000JOL", "Job Openings Level", "demand", "NSA", 7, "#7bd389"),
    LaborSeriesDefinition("JTS000000000000000QUL", "Quits Level", "demand", "NSA", 8, "#66c7f4"),
    LaborSeriesDefinition("JTS000000000000000HIL", "Hires Level", "demand", "NSA", 9, "#4ecdc4"),
    LaborSeriesDefinition("JTS000000000000000TSL", "Total Separations Level", "demand", "NSA", 10, "#a5d6ff"),
)


class BLSLaborClient:
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
    ) -> dict[str, list[LaborObservation]]:
        payload = {
            "seriesid": list(series_ids),
            "startyear": str(start_year),
            "endyear": str(end_year),
        }
        response = self.post_json(self.api_url, payload)
        if not isinstance(response, Mapping):
            raise LaborIngestionError("BLS endpoint returned a non-object payload.")
        if str(response.get("status", "")).upper() != "REQUEST_SUCCEEDED":
            raise LaborIngestionError(f"BLS request failed: {response.get('message')}")

        results = response.get("Results")
        if not isinstance(results, Mapping):
            raise LaborIngestionError("BLS payload missing Results object.")
        raw_series = results.get("series")
        if not isinstance(raw_series, list):
            raise LaborIngestionError("BLS payload missing series array.")

        observations_by_series: dict[str, list[LaborObservation]] = {}
        for raw_series_item in raw_series:
            if not isinstance(raw_series_item, Mapping):
                continue
            series_id = str(raw_series_item.get("seriesID") or "").strip()
            if not series_id:
                continue

            parsed: list[LaborObservation] = []
            raw_points = raw_series_item.get("data")
            if not isinstance(raw_points, list):
                continue
            for point in raw_points:
                if not isinstance(point, Mapping):
                    continue
                period = str(point.get("period") or "")
                if not period.startswith("M") or len(period) != 3:
                    continue
                value = _coerce_float(point.get("value"))
                if value is None:
                    continue
                year = int(str(point.get("year") or "0"))
                month = int(period[1:])
                obs_date = f"{year:04d}-{month:02d}-01"
                parsed.append(
                    LaborObservation(
                        series_id=series_id,
                        observation_date=obs_date,
                        year=year,
                        period=period,
                        period_name=str(point.get("periodName") or period),
                        value=value,
                        footnotes=tuple(_extract_footnotes(point.get("footnotes"))),
                    )
                )
            parsed.sort(key=lambda item: item.observation_date)
            observations_by_series[series_id] = parsed
        return observations_by_series


class LaborStore:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._open_connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS labor_series_catalog (
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

                CREATE TABLE IF NOT EXISTS labor_observations (
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
                CREATE INDEX IF NOT EXISTS idx_labor_observations_date
                ON labor_observations (observation_date DESC);

                CREATE TABLE IF NOT EXISTS labor_metrics (
                    metric_date TEXT NOT NULL,
                    metric_key TEXT NOT NULL,
                    metric_value REAL NOT NULL,
                    metadata_json TEXT NOT NULL,
                    computed_at TEXT NOT NULL,
                    PRIMARY KEY (metric_date, metric_key)
                );

                CREATE TABLE IF NOT EXISTS labor_ingestion_runs (
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

    def write_series_catalog(self, series_definitions: Iterable[LaborSeriesDefinition]) -> None:
        self.initialize()
        updated_at = _utcnow().isoformat()
        with self._open_connection() as connection:
            connection.executemany(
                """
                INSERT OR REPLACE INTO labor_series_catalog (
                    series_id, label, category, seasonal_adjustment, display_order, color,
                    active, metadata_json, updated_at
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

    def write_observations(self, observations: Iterable[LaborObservation]) -> int:
        self.initialize()
        fetched_at = _utcnow().isoformat()
        values = list(observations)
        if not values:
            return 0
        with self._open_connection() as connection:
            connection.executemany(
                """
                INSERT OR REPLACE INTO labor_observations (
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
                INSERT OR REPLACE INTO labor_metrics (
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
                INSERT OR REPLACE INTO labor_ingestion_runs (
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
                    FROM labor_observations o
                    JOIN labor_series_catalog c ON c.series_id = o.series_id
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
                    source=f"labor_{series_id}_{observation_date.replace('-', '')}",
                    content=(
                        f"Labor observation for {label} ({series_id}) on {observation_date}: "
                        f"value {value:.3f}."
                    ),
                    kind="labor_observation",
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


class LaborIngestionService:
    def __init__(
        self,
        store: LaborStore,
        client: BLSLaborClient | None = None,
        series_definitions: Iterable[LaborSeriesDefinition] = LABOR_SERIES_DEFINITIONS,
    ) -> None:
        self.store = store
        self.client = client or BLSLaborClient()
        self.series_definitions = tuple(series_definitions)

    def ingest(self, *, start_year: int, end_year: int) -> dict[str, object]:
        if start_year > end_year:
            raise LaborIngestionError("start_year cannot be greater than end_year.")

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
            all_observations = [obs for items in observations_by_series.values() for obs in items]
            observation_count = self.store.write_observations(all_observations)

            metric_date, metrics, metadata_by_key = _compute_latest_labor_metrics(observations_by_series)
            if metric_date:
                self.store.write_metrics(metric_date=metric_date, metrics=metrics, metadata_by_key=metadata_by_key)

            self.store.record_run(
                run_id=run_id,
                started_at=started_at,
                completed_at=_utcnow().isoformat(),
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


def export_dashboard_labor_js(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    output_js_path: str | Path,
) -> dict[str, object] | None:
    payload = build_dashboard_labor_payload(db_path=db_path)
    output_path = Path(output_js_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if payload is None:
        output_path.write_text("window.__LABOR_DASHBOARD_DATA__ = null;\n", encoding="utf-8")
        return None
    output_path.write_text(
        "window.__LABOR_DASHBOARD_DATA__ = " + json.dumps(payload, sort_keys=True, indent=2) + ";\n",
        encoding="utf-8",
    )
    return payload


def export_dashboard_labor_kg_js(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    output_js_path: str | Path,
    months: int = 24,
) -> dict[str, object] | None:
    payload = build_labor_knowledge_graph_payload(db_path=db_path, months=months)
    output_path = Path(output_js_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if payload is None:
        output_path.write_text("window.__LABOR_KG_DASHBOARD_DATA__ = null;\n", encoding="utf-8")
        return None
    output_path.write_text(
        "window.__LABOR_KG_DASHBOARD_DATA__ = " + json.dumps(payload, sort_keys=True, indent=2) + ";\n",
        encoding="utf-8",
    )
    return payload


def build_dashboard_labor_payload(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, object] | None:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        metric_date_row = connection.execute(
            """
            SELECT metric_date
            FROM labor_metrics
            WHERE metric_key = 'labor_bias'
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
            FROM labor_metrics
            WHERE metric_date = ?
            """,
            (metric_date,),
        ).fetchall()

        value_rows = connection.execute(
            """
            SELECT o.series_id, o.value, c.label, c.category
            FROM labor_observations o
            JOIN labor_series_catalog c ON c.series_id = o.series_id
            WHERE o.observation_date = ?
            ORDER BY c.display_order ASC
            """,
            (metric_date,),
        ).fetchall()
    finally:
        connection.close()

    metrics = {str(row["metric_key"]): round(float(row["metric_value"]), 6) for row in metric_rows}
    metadata = {str(row["metric_key"]): json.loads(row["metadata_json"] or "{}") for row in metric_rows}
    heat_card = _build_labor_heat_card(metric_date=metric_date, metrics=metrics)
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
        "bias_history": build_labor_bias_history(db_path=db_path),
        "source_url": "https://api.bls.gov/publicAPI/v2/timeseries/data/",
    }


def build_labor_bias_history(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> list[dict[str, object]]:
    """Replay the labor bias formula across every stored month, oldest first.

    `labor_metrics` only retains the latest reading, so anything that needs the
    dispersion of the labor signal has no history to measure. JOLTS series lag the
    household/establishment surveys, so they are looked up on-or-before each date.
    """
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT series_id, observation_date, value
            FROM labor_observations
            ORDER BY observation_date ASC
            """
        ).fetchall()
    except sqlite3.DatabaseError:
        return []
    finally:
        connection.close()

    lookups: dict[str, dict[str, float]] = {}
    for row in rows:
        value = _coerce_float(row["value"])
        if value is None:
            continue
        lookups.setdefault(str(row["series_id"]), {})[str(row["observation_date"])] = value

    unemployment = lookups.get("LNS14000000", {})
    participation = lookups.get("LNS11300000", {})
    payrolls = lookups.get("CES0000000001", {})
    wages = lookups.get("CES0500000003", {})
    unemployed_level = lookups.get("LNS13000000", {})
    openings = lookups.get("JTS000000000000000JOL", {})

    core = [unemployment, participation, payrolls, wages]
    if any(not series for series in core):
        return []

    history: list[dict[str, object]] = []
    for metric_date in sorted(set(unemployment) & set(participation) & set(payrolls) & set(wages)):
        payroll_level = payrolls.get(metric_date)
        deltas = []
        for offset in (0, 1, 2):
            current = payrolls.get(_shift_month(metric_date, -offset))
            previous = payrolls.get(_shift_month(metric_date, -offset - 1))
            if current is not None and previous is not None:
                deltas.append(current - previous)
        payroll_3m_avg = _mean_available(deltas) if deltas else None

        wages_yoy = _pct_change(wages.get(metric_date), wages.get(_shift_month(metric_date, -12)))
        openings_level = _lookup_on_or_before(openings, metric_date)
        unemployed_value = _lookup_on_or_before(unemployed_level, metric_date)
        openings_unemployed_ratio = None
        if openings_level not in (None, 0.0) and unemployed_value not in (None, 0.0):
            openings_unemployed_ratio = openings_level / unemployed_value

        if payroll_level is None and wages_yoy is None:
            continue
        history.append(
            {
                "date": metric_date,
                "bias": _labor_bias_from_inputs(
                    payroll_3m_avg=payroll_3m_avg,
                    unemployment_rate=unemployment.get(metric_date),
                    wages_yoy=wages_yoy,
                    openings_unemployed_ratio=openings_unemployed_ratio,
                    participation_rate=participation.get(metric_date),
                ),
                "unemployment_rate": unemployment.get(metric_date),
                "payroll_3m_avg": None if payroll_3m_avg is None else round(payroll_3m_avg, 4),
                "wages_yoy": wages_yoy,
            }
        )
    return history


def build_labor_knowledge_graph_payload(
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
            FROM labor_series_catalog
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
                FROM labor_observations o
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
            FROM labor_metrics
            WHERE metric_key='labor_bias'
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
                FROM labor_metrics
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
    observation_count = 0

    series_ids = [str(row["series_id"]) for row in series_rows]
    z_span = max(1.0, (len(series_ids) - 1) * 1.2)
    z_start = -z_span / 2
    cluster_x = -7.0
    for series_idx, series_id in enumerate(series_ids):
        info = series_lookup[series_id]
        series_node_id = f"labor-series:{series_id}"
        series_z = z_start + series_idx * 1.2
        series_pos = (cluster_x, 1.1, series_z)
        nodes.append(
            {
                "id": series_node_id,
                "kind": "labor_series",
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
        for idx, item in enumerate(observations):
            observation_count += 1
            obs_id = f"labor-obs:{series_id}:{item['observation_date']}"
            x = cluster_x - 3.2 + (idx / max(1, len(observations) - 1)) * 6.4
            y = -2.3 + ((float(item["value"]) - min_value) / value_range) * 2.8
            z = series_z + (0.06 if idx % 2 == 0 else -0.06)
            nodes.append(
                {
                    "id": obs_id,
                    "kind": "labor_observation",
                    "label": str(item["observation_date"]),
                    "sublabel": f"{float(item['value']):.1f}",
                    "x": round(x, 3),
                    "y": round(y, 3),
                    "z": round(z, 3),
                    "size": 0.055 if idx < len(observations) - 1 else 0.085,
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
        heat_card = _build_labor_heat_card(metric_date=metric_date, metrics=metrics)
        metric_anchor = ("labor-metric-anchor", -4.8, 2.9, 0.0)
        nodes.append(
            {
                "id": metric_anchor[0],
                "kind": "labor_metric",
                "label": "Labor Composite",
                "sublabel": str(heat_card["display"]),
                "x": metric_anchor[1],
                "y": metric_anchor[2],
                "z": metric_anchor[3],
                "size": 0.22,
                "color": _tone_hex(str(heat_card["tone"])),
            }
        )
        edges.append(
            {
                "source": metric_anchor[0],
                "target": "fomc-hub",
                "color": "#7bd389",
                "opacity": 0.28,
            }
        )
        for idx, row in enumerate(metric_rows):
            metric_id = f"labor-metric:{str(row['metric_key'])}"
            metric_x = -3.8 - idx * 0.48
            metric_y = 3.7
            metric_z = -1.2 + idx * 0.35
            nodes.append(
                {
                    "id": metric_id,
                    "kind": "labor_metric",
                    "label": str(row["metric_key"]),
                    "sublabel": f"{float(row['metric_value']):.3f}",
                    "x": round(metric_x, 3),
                    "y": round(metric_y, 3),
                    "z": round(metric_z, 3),
                    "size": 0.08,
                    "color": "#7bd389",
                }
            )
            edges.append(
                {
                    "source": metric_anchor[0],
                    "target": metric_id,
                    "color": "#7bd389",
                    "opacity": 0.24,
                }
            )

    return {
        "generated_at": _utcnow().isoformat(),
        "metric_date": metric_date,
        "labor_heat_card": heat_card,
        "labor_graph": {
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
    parser = argparse.ArgumentParser(description="Ingest labor time series data from BLS into SQLite.")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="SQLite database path.")
    parser.add_argument("--start-year", type=int, default=date.today().year - 2, help="First year to ingest.")
    parser.add_argument("--end-year", type=int, default=date.today().year, help="Last year to ingest.")
    parser.add_argument("--dashboard-js", help="Optional output path for window.__LABOR_DASHBOARD_DATA__ payload.")
    parser.add_argument(
        "--kg-js",
        help="Optional output path for window.__LABOR_KG_DASHBOARD_DATA__ payload with labor nodes/edges.",
    )
    parser.add_argument(
        "--kg-months",
        type=int,
        default=24,
        help="Months of labor points to include in KG export (default: 24).",
    )
    args = parser.parse_args(argv)

    store = LaborStore(args.db_path)
    service = LaborIngestionService(store=store)
    result = service.ingest(start_year=args.start_year, end_year=args.end_year)

    if args.dashboard_js:
        export_dashboard_labor_js(db_path=args.db_path, output_js_path=args.dashboard_js)
    if args.kg_js:
        export_dashboard_labor_kg_js(db_path=args.db_path, output_js_path=args.kg_js, months=args.kg_months)

    print(
        f"Ingested {result['observation_count']} labor observations across {result['series_count']} series "
        f"(metric_date={result['metric_date']})."
    )
    return 0


def _compute_latest_labor_metrics(
    observations_by_series: Mapping[str, list[LaborObservation]],
) -> tuple[str | None, dict[str, float], dict[str, Mapping[str, object]]]:
    unemployment = _observation_lookup(observations_by_series.get("LNS14000000", []))
    participation = _observation_lookup(observations_by_series.get("LNS11300000", []))
    prime_epop = _observation_lookup(observations_by_series.get("LNS12300060", []))
    payrolls = _observation_lookup(observations_by_series.get("CES0000000001", []))
    wages = _observation_lookup(observations_by_series.get("CES0500000003", []))
    unemployed_level = _observation_lookup(observations_by_series.get("LNS13000000", []))
    openings = _observation_lookup(observations_by_series.get("JTS000000000000000JOL", []))
    quits = _observation_lookup(observations_by_series.get("JTS000000000000000QUL", []))
    hires = _observation_lookup(observations_by_series.get("JTS000000000000000HIL", []))

    core_lookups = [unemployment, participation, payrolls, wages]
    if any(not item for item in core_lookups):
        return None, {}, {}

    demand_lookups = [series for series in (openings, quits, hires, unemployed_level, prime_epop) if series]
    metric_date = _latest_common_date(core_lookups + demand_lookups) or _latest_common_date(core_lookups)
    if metric_date is None:
        return None, {}, {}

    unemployment_rate = unemployment.get(metric_date)
    participation_rate = participation.get(metric_date)
    prime_epop_rate = _lookup_on_or_before(prime_epop, metric_date)
    payroll_level = payrolls.get(metric_date)
    payroll_prev_1 = payrolls.get(_shift_month(metric_date, -1))
    payroll_prev_2 = payrolls.get(_shift_month(metric_date, -2))
    payroll_prev_3 = payrolls.get(_shift_month(metric_date, -3))
    payroll_mom = (payroll_level - payroll_prev_1) if (payroll_level is not None and payroll_prev_1 is not None) else None
    payroll_mom_prev_1 = (payroll_prev_1 - payroll_prev_2) if (payroll_prev_1 is not None and payroll_prev_2 is not None) else None
    payroll_mom_prev_2 = (payroll_prev_2 - payroll_prev_3) if (payroll_prev_2 is not None and payroll_prev_3 is not None) else None
    payroll_3m_avg = _mean_available([payroll_mom, payroll_mom_prev_1, payroll_mom_prev_2])

    wages_yoy = _pct_change(wages.get(metric_date), wages.get(_shift_month(metric_date, -12)))
    wages_mom = _pct_change(wages.get(metric_date), wages.get(_shift_month(metric_date, -1)))
    wages_3m_ann = _annualized_change(wages.get(metric_date), wages.get(_shift_month(metric_date, -3)), 3)

    openings_level = _lookup_on_or_before(openings, metric_date)
    quits_level = _lookup_on_or_before(quits, metric_date)
    hires_level = _lookup_on_or_before(hires, metric_date)
    unemployed_level_value = _lookup_on_or_before(unemployed_level, metric_date)

    openings_unemployed_ratio = None
    if openings_level not in (None, 0.0) and unemployed_level_value not in (None, 0.0):
        openings_unemployed_ratio = openings_level / unemployed_level_value
    quits_hires_ratio = None
    if quits_level not in (None, 0.0) and hires_level not in (None, 0.0):
        quits_hires_ratio = quits_level / hires_level

    labor_bias = _labor_bias_from_inputs(
        payroll_3m_avg=payroll_3m_avg,
        unemployment_rate=unemployment_rate,
        wages_yoy=wages_yoy,
        openings_unemployed_ratio=openings_unemployed_ratio,
        participation_rate=participation_rate,
    )
    labor_heat_score = float(_score_from_bias(labor_bias))

    metrics: dict[str, float] = {
        "labor_bias": labor_bias,
        "labor_heat_score": labor_heat_score,
    }
    optional_metrics = {
        "unemployment_rate": unemployment_rate,
        "participation_rate": participation_rate,
        "prime_age_epop_rate": prime_epop_rate,
        "payroll_mom": payroll_mom,
        "payroll_3m_avg": payroll_3m_avg,
        "wages_yoy": wages_yoy,
        "wages_mom": wages_mom,
        "wages_3m_annualized": wages_3m_ann,
        "job_openings_level": openings_level,
        "quits_level": quits_level,
        "hires_level": hires_level,
        "openings_unemployed_ratio": openings_unemployed_ratio,
        "quits_hires_ratio": quits_hires_ratio,
    }
    for key, value in optional_metrics.items():
        if value is not None:
            metrics[key] = float(round(value, 6))

    metadata_by_key: dict[str, Mapping[str, object]] = {
        "labor_bias": {
            "formula": (
                "0.30*payroll_3m + 0.25*unemployment + 0.20*wages + "
                "0.15*openings_unemployed + 0.10*participation"
            ),
            "weights": {
                "payroll_3m": 0.30,
                "unemployment": 0.25,
                "wages": 0.20,
                "openings_unemployed": 0.15,
                "participation": 0.10,
            },
            "neutral_assumptions": {
                "payroll_3m_avg_k": 100.0,
                "unemployment_rate": 4.2,
                "wages_yoy": 0.03,
                "openings_unemployed_ratio": 1.0,
                "participation_rate": 62.0,
            },
        }
    }
    return metric_date, metrics, metadata_by_key


def _labor_bias_from_inputs(
    *,
    payroll_3m_avg: float | None,
    unemployment_rate: float | None,
    wages_yoy: float | None,
    openings_unemployed_ratio: float | None,
    participation_rate: float | None,
) -> float:
    payroll_component = _clamp(((payroll_3m_avg or 100.0) - 100.0) / 150.0, -1.0, 1.0)
    unemployment_component = _clamp((4.2 - (unemployment_rate or 4.2)) / 1.4, -1.0, 1.0)
    wage_component = _clamp(((wages_yoy or 0.03) - 0.03) / 0.02, -1.0, 1.0)
    openings_component = _clamp(((openings_unemployed_ratio or 1.0) - 1.0) / 0.6, -1.0, 1.0)
    participation_component = _clamp(((participation_rate or 62.0) - 62.0) / 1.5, -1.0, 1.0)
    return round(
        _clamp(
            (0.30 * payroll_component)
            + (0.25 * unemployment_component)
            + (0.20 * wage_component)
            + (0.15 * openings_component)
            + (0.10 * participation_component),
            -1.0,
            1.0,
        ),
        4,
    )


def _build_labor_heat_card(*, metric_date: str, metrics: Mapping[str, float]) -> dict[str, object]:
    bias = float(metrics.get("labor_bias", 0.0))
    score = _score_from_bias(bias)
    tone = _tone_from_score(score)
    sign = "+" if bias > 0 else ""
    display = f"{sign}{bias:.2f}"
    source_pills = [f"BLS Labor {metric_date}", f"bias {display}"]
    if "unemployment_rate" in metrics:
        source_pills.append(f"u3 {metrics['unemployment_rate']:.1f}%")
    if "payroll_3m_avg" in metrics:
        source_pills.append(f"payroll 3m avg {metrics['payroll_3m_avg']:.0f}k")
    if "wages_yoy" in metrics:
        source_pills.append(f"wages YoY {metrics['wages_yoy'] * 100:.2f}%")
    if "openings_unemployed_ratio" in metrics:
        source_pills.append(f"openings/unemployed {metrics['openings_unemployed_ratio']:.2f}")
    return {
        "label": "Labor Momentum",
        "display": display,
        "score": score,
        "tone": tone,
        "toneLabel": "employment",
        "sources": source_pills,
    }


def _observation_lookup(observations: Iterable[LaborObservation]) -> dict[str, float]:
    return {item.observation_date: item.value for item in observations}


def _lookup_on_or_before(lookup: Mapping[str, float], iso_date: str) -> float | None:
    candidates = [key for key in lookup.keys() if key <= iso_date]
    if not candidates:
        return None
    latest = max(candidates)
    return lookup.get(latest)


def _latest_common_date(lookups: Iterable[Mapping[str, float]]) -> str | None:
    keys: set[str] | None = None
    for lookup in lookups:
        current_keys = set(lookup.keys())
        if keys is None:
            keys = current_keys
        else:
            keys &= current_keys
    if not keys:
        return None
    return sorted(keys)[-1]


def _mean_available(values: Iterable[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return sum(present) / len(present)


def _pct_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous in (None, 0.0):
        return None
    return round((current / previous) - 1.0, 6)


def _annualized_change(current: float | None, previous: float | None, months: int) -> float | None:
    if current is None or previous in (None, 0.0) or months <= 0:
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


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


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
        raise LaborIngestionError(f"Expected JSON object or array from {url}.")
    return body


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


if __name__ == "__main__":
    raise SystemExit(main())
