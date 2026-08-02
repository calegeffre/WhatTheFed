"""BLS Producer Price Index ingestion and dashboard export."""

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


DEFAULT_DB_PATH = Path("data") / "market_snapshots.db"
BLS_API_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
PROVIDER = "BLS"


@dataclass(frozen=True)
class PPISeriesDefinition:
    series_id: str
    label: str
    category: str
    display_order: int
    color: str


@dataclass(frozen=True)
class PPIObservation:
    series_id: str
    observation_date: str
    value: float
    preliminary: bool = False


PPI_SERIES_DEFINITIONS: tuple[PPISeriesDefinition, ...] = (
    PPISeriesDefinition("WPSFD4", "PPI Final Demand (SA)", "headline", 1, "#f97583"),
    PPISeriesDefinition("WPSFD49116", "PPI Core Final Demand (SA)", "core", 2, "#ff9f43"),
    PPISeriesDefinition("WPSFD411", "PPI Final Demand Goods (SA)", "goods", 3, "#58a6ff"),
    PPISeriesDefinition("WPSFD412", "PPI Final Demand Trade Services (SA)", "trade", 4, "#d2a8ff"),
    PPISeriesDefinition("WPSFD413", "PPI Final Demand Services (SA)", "services", 5, "#7bd389"),
)

JsonPoster = Callable[[str, Mapping[str, object]], Mapping[str, object] | list[object]]


class PPIIngestionError(RuntimeError):
    pass


class BLSPPIClient:
    def __init__(self, post_json: JsonPoster | None = None, api_url: str = BLS_API_URL) -> None:
        self.post_json = post_json or _post_json
        self.api_url = api_url

    def fetch_observations(
        self, *, series_ids: Iterable[str], start_year: int, end_year: int
    ) -> dict[str, list[PPIObservation]]:
        response = self.post_json(
            self.api_url,
            {"seriesid": list(series_ids), "startyear": str(start_year), "endyear": str(end_year)},
        )
        if not isinstance(response, Mapping) or str(response.get("status", "")).upper() != "REQUEST_SUCCEEDED":
            raise PPIIngestionError(f"BLS PPI request failed: {response}")
        results = response.get("Results")
        rows = results.get("series") if isinstance(results, Mapping) else None
        if not isinstance(rows, list):
            raise PPIIngestionError("BLS PPI payload did not include Results.series.")

        by_series: dict[str, list[PPIObservation]] = {}
        for series in rows:
            if not isinstance(series, Mapping):
                continue
            series_id = str(series.get("seriesID") or "")
            points: list[PPIObservation] = []
            for point in series.get("data") or []:
                if not isinstance(point, Mapping):
                    continue
                period = str(point.get("period") or "")
                value = _float(point.get("value"))
                if len(period) != 3 or not period.startswith("M") or value is None:
                    continue
                year = int(str(point.get("year") or "0"))
                month = int(period[1:])
                footnotes = point.get("footnotes") or []
                preliminary = any(
                    isinstance(item, Mapping) and str(item.get("code") or "").upper() == "P"
                    for item in footnotes
                )
                points.append(
                    PPIObservation(series_id, f"{year:04d}-{month:02d}-01", value, preliminary)
                )
            by_series[series_id] = sorted(points, key=lambda item: item.observation_date)
        return by_series


class PPIStore:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS ppi_series_catalog (
                    series_id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    category TEXT NOT NULL,
                    display_order INTEGER NOT NULL,
                    color TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ppi_observations (
                    series_id TEXT NOT NULL,
                    observation_date TEXT NOT NULL,
                    value REAL NOT NULL,
                    preliminary INTEGER NOT NULL,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (series_id, observation_date)
                );
                CREATE INDEX IF NOT EXISTS idx_ppi_observation_date
                    ON ppi_observations (observation_date DESC);
                CREATE TABLE IF NOT EXISTS ppi_ingestion_runs (
                    run_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    observation_count INTEGER,
                    error_message TEXT
                );
                """
            )

    def write_catalog(self, definitions: Iterable[PPISeriesDefinition]) -> None:
        self.initialize()
        now = _now()
        with self._connection() as connection:
            connection.executemany(
                """
                INSERT OR REPLACE INTO ppi_series_catalog
                    (series_id, label, category, display_order, color, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [(d.series_id, d.label, d.category, d.display_order, d.color, now) for d in definitions],
            )

    def write_observations(self, observations: Iterable[PPIObservation]) -> int:
        self.initialize()
        values = list(observations)
        now = _now()
        with self._connection() as connection:
            connection.executemany(
                """
                INSERT OR REPLACE INTO ppi_observations
                    (series_id, observation_date, value, preliminary, fetched_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                [(o.series_id, o.observation_date, o.value, int(o.preliminary), now) for o in values],
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
        error_message: str | None = None,
    ) -> None:
        self.initialize()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO ppi_ingestion_runs
                    (run_id, started_at, completed_at, status, observation_count, error_message)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, started_at, completed_at, status, observation_count, error_message),
            )

    @contextlib.contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.db_path)
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()


class PPIIngestionService:
    def __init__(
        self,
        store: PPIStore,
        client: BLSPPIClient | None = None,
        definitions: Iterable[PPISeriesDefinition] = PPI_SERIES_DEFINITIONS,
    ) -> None:
        self.store = store
        self.client = client or BLSPPIClient()
        self.definitions = tuple(definitions)

    def ingest(self, *, start_year: int, end_year: int) -> dict[str, object]:
        if start_year > end_year:
            raise PPIIngestionError("start_year cannot be greater than end_year.")
        run_id, started = str(uuid.uuid4()), _now()
        self.store.record_run(run_id=run_id, started_at=started, status="started")
        try:
            self.store.write_catalog(self.definitions)
            by_series = self.client.fetch_observations(
                series_ids=[item.series_id for item in self.definitions],
                start_year=start_year,
                end_year=end_year,
            )
            count = self.store.write_observations(
                point for points in by_series.values() for point in points
            )
            latest = max(
                (point.observation_date for points in by_series.values() for point in points),
                default=None,
            )
            self.store.record_run(
                run_id=run_id,
                started_at=started,
                completed_at=_now(),
                status="completed",
                observation_count=count,
            )
            return {"run_id": run_id, "observation_count": count, "metric_date": latest}
        except Exception as exc:
            self.store.record_run(
                run_id=run_id,
                started_at=started,
                completed_at=_now(),
                status="failed",
                error_message=str(exc),
            )
            raise


def ppi_bias(*, headline_yoy: float | None, core_yoy: float | None, core_3m: float | None) -> float:
    values = [(headline_yoy, 0.25), (core_yoy, 0.45), (core_3m, 0.30)]
    present = [(value, weight) for value, weight in values if value is not None]
    if not present:
        return 0.0
    inflation = sum(value * weight for value, weight in present) / sum(weight for _, weight in present)
    return round(max(-1.0, min(1.0, (inflation - 2.0) / 2.0)), 4)


def build_ppi_bias_history(
    *, db_path: str | Path = DEFAULT_DB_PATH, headline_id: str = "WPSFD4", core_id: str = "WPSFD49116"
) -> list[dict[str, object]]:
    try:
        connection = sqlite3.connect(db_path)
        rows = connection.execute(
            """
            SELECT series_id, observation_date, value FROM ppi_observations
            WHERE series_id IN (?, ?) ORDER BY observation_date
            """,
            (headline_id, core_id),
        ).fetchall()
    except sqlite3.DatabaseError:
        return []
    finally:
        if "connection" in locals():
            connection.close()
    values: dict[str, dict[str, float]] = {headline_id: {}, core_id: {}}
    for series_id, observation_date, value in rows:
        values[str(series_id)][str(observation_date)] = float(value)
    history = []
    for observation_date in sorted(set(values[headline_id]).intersection(values[core_id])):
        headline_yoy = _pct(values[headline_id], observation_date, 12)
        core_yoy = _pct(values[core_id], observation_date, 12)
        core_3m = _annualized(values[core_id], observation_date, 3)
        if headline_yoy is None and core_yoy is None and core_3m is None:
            continue
        history.append(
            {
                "date": observation_date,
                "bias": ppi_bias(headline_yoy=headline_yoy, core_yoy=core_yoy, core_3m=core_3m),
                "headline_yoy": headline_yoy,
                "core_yoy": core_yoy,
                "core_3m_annualized": core_3m,
            }
        )
    return history


def build_dashboard_ppi_payload(*, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, object] | None:
    history = build_ppi_bias_history(db_path=db_path)
    if not history:
        return None
    latest = history[-1]
    metric_date = str(latest["date"])
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT o.series_id, o.value, o.preliminary, c.label, c.category
            FROM ppi_observations o JOIN ppi_series_catalog c USING(series_id)
            WHERE o.observation_date = ? ORDER BY c.display_order
            """,
            (metric_date,),
        ).fetchall()
    finally:
        connection.close()
    metrics = {
        "headline_yoy": latest["headline_yoy"],
        "core_yoy": latest["core_yoy"],
        "core_3m_annualized": latest["core_3m_annualized"],
        "ppi_bias": latest["bias"],
    }
    bias = float(latest["bias"])
    score = _score(bias)
    return {
        "generated_at": _now(),
        "metric_date": metric_date,
        "provider": PROVIDER,
        "metrics": metrics,
        "metric_metadata": {
            "ppi_bias": {
                "formula": "weighted headline YoY/core YoY/core 3m annualized; neutral 2.0%",
                "weights": {"headline_yoy": 0.25, "core_yoy": 0.45, "core_3m_annualized": 0.30},
            }
        },
        "heat_card": {
            "label": "Producer Prices",
            "display": f"{bias:+.2f}",
            "score": score,
            "tone": _tone(score),
            "toneLabel": "PPI pressure",
            "sources": [
                f"BLS {metric_date}",
                f"headline YoY {_format_pct(latest['headline_yoy'])}",
                f"core YoY {_format_pct(latest['core_yoy'])}",
            ],
        },
        "latest_values": [
            {
                "series_id": str(row["series_id"]),
                "label": str(row["label"]),
                "category": str(row["category"]),
                "value": float(row["value"]),
                "preliminary": bool(row["preliminary"]),
            }
            for row in rows
        ],
        "bias_history": history,
        "source_url": BLS_API_URL,
    }


def build_ppi_knowledge_graph_payload(
    *, db_path: str | Path = DEFAULT_DB_PATH, per_series_limit: int = 8
) -> dict[str, object] | None:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        series = connection.execute(
            "SELECT * FROM ppi_series_catalog ORDER BY display_order"
        ).fetchall()
        observations = connection.execute(
            """
            WITH ranked AS (
              SELECT *, ROW_NUMBER() OVER(PARTITION BY series_id ORDER BY observation_date DESC) rank
              FROM ppi_observations
            )
            SELECT series_id, observation_date, value FROM ranked WHERE rank <= ?
            ORDER BY series_id, observation_date
            """,
            (max(1, per_series_limit),),
        ).fetchall()
    except sqlite3.DatabaseError:
        return None
    finally:
        connection.close()
    if not series or not observations:
        return None
    nodes = [
        {
            "id": "ppi-hub",
            "kind": "ppi_hub",
            "label": "Producer Prices",
            "sublabel": "BLS PPI",
            "x": 0,
            "y": 4.2,
            "z": 3.4,
            "size": 0.24,
            "color": "#f97583",
        }
    ]
    edges = [{"source": "ppi-hub", "target": "fomc-hub", "color": "#f97583", "opacity": 0.3}]
    by_series: dict[str, list[sqlite3.Row]] = {}
    for row in observations:
        by_series.setdefault(str(row["series_id"]), []).append(row)
    count = 0
    for index, row in enumerate(series):
        series_id = str(row["series_id"])
        node_id = f"ppi-series-{series_id.lower()}"
        x = -3 + index * 1.5
        color = str(row["color"])
        nodes.append(
            {
                "id": node_id,
                "kind": "ppi_series",
                "label": str(row["label"]),
                "sublabel": series_id,
                "x": x,
                "y": 3.3,
                "z": 3.4,
                "size": 0.14,
                "color": color,
            }
        )
        edges.append({"source": "ppi-hub", "target": node_id, "color": color, "opacity": 0.28})
        previous = None
        for depth, observation in enumerate(by_series.get(series_id, [])):
            obs_id = f"ppi-{series_id.lower()}-{observation['observation_date']}"
            count += 1
            nodes.append(
                {
                    "id": obs_id,
                    "kind": "ppi_observation",
                    "label": str(observation["observation_date"]),
                    "sublabel": f"{float(observation['value']):.3f}",
                    "x": x,
                    "y": round(3 - depth * 0.3, 3),
                    "z": round(3.7 + depth * 0.3, 3),
                    "size": 0.06,
                    "color": color,
                }
            )
            edges.append({"source": node_id, "target": obs_id, "color": color, "opacity": 0.12})
            if previous:
                edges.append({"source": previous, "target": obs_id, "color": color, "opacity": 0.22})
            previous = obs_id
    return {
        "ppi_graph": {
            "nodes": nodes,
            "edges": edges,
            "stats": {"series_count": len(series), "observation_count": count, "edge_count": len(edges)},
        }
    }


def export_dashboard_ppi_js(
    *, db_path: str | Path = DEFAULT_DB_PATH, output_js_path: str | Path, per_series_limit: int = 8
) -> dict[str, object] | None:
    payload = build_dashboard_ppi_payload(db_path=db_path)
    output = Path(output_js_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if payload is None:
        output.write_text("window.__PPI_DASHBOARD_DATA__ = null;\n", encoding="utf-8")
        return None
    graph = build_ppi_knowledge_graph_payload(db_path=db_path, per_series_limit=per_series_limit)
    if graph:
        payload = {**payload, **graph}
    output.write_text(
        "window.__PPI_DASHBOARD_DATA__ = " + json.dumps(payload, indent=2, sort_keys=True) + ";\n",
        encoding="utf-8",
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest BLS Producer Price Index data.")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--start-year", type=int, default=date.today().year - 3)
    parser.add_argument("--end-year", type=int, default=date.today().year)
    parser.add_argument("--dashboard-js")
    args = parser.parse_args(argv)
    result = PPIIngestionService(PPIStore(args.db_path)).ingest(
        start_year=args.start_year, end_year=args.end_year
    )
    if args.dashboard_js:
        export_dashboard_ppi_js(db_path=args.db_path, output_js_path=args.dashboard_js)
    print(
        f"Ingested {result['observation_count']} PPI observations "
        f"(latest={result['metric_date']})."
    )
    return 0


def _shift_month(value: str, months: int) -> str:
    current = datetime.strptime(value, "%Y-%m-%d")
    index = current.year * 12 + current.month - 1 - months
    return f"{index // 12:04d}-{index % 12 + 1:02d}-01"


def _pct(values: Mapping[str, float], observation_date: str, months: int) -> float | None:
    current, prior = values.get(observation_date), values.get(_shift_month(observation_date, months))
    if current is None or prior in (None, 0):
        return None
    return round((current / prior - 1) * 100, 6)


def _annualized(values: Mapping[str, float], observation_date: str, months: int) -> float | None:
    current, prior = values.get(observation_date), values.get(_shift_month(observation_date, months))
    if current is None or prior is None or prior <= 0:
        return None
    return round(((current / prior) ** (12 / months) - 1) * 100, 6)


def _score(bias: float) -> int:
    return max(1, min(5, round(3 + bias * 2)))


def _tone(score: int) -> str:
    return {1: "cold", 2: "cool", 3: "balanced", 4: "warm", 5: "hot"}[score]


def _format_pct(value: object) -> str:
    return "n/a" if value is None else f"{float(value):.2f}%"


def _float(value: object) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _post_json(url: str, payload: Mapping[str, object]) -> Mapping[str, object] | list[object]:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "WhatTheFed/1.0"},
    )
    with urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
