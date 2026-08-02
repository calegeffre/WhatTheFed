"""Treasury Fiscal Data ingestion using Monthly Treasury Statement table 1."""

from __future__ import annotations

import argparse
import contextlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_DB_PATH = Path("data") / "market_snapshots.db"
FISCAL_API_URL = (
    "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/"
    "v1/accounting/mts/mts_table_1"
)
FISCAL_HOME_URL = (
    "https://fiscaldata.treasury.gov/datasets/monthly-treasury-statement/"
    "summary-of-receipts-outlays-and-the-deficit-surplus-of-the-u-s-government"
)
MONTHS = {
    "January": 1,
    "February": 2,
    "March": 3,
    "April": 4,
    "May": 5,
    "June": 6,
    "July": 7,
    "August": 8,
    "September": 9,
    "October": 10,
    "November": 11,
    "December": 12,
}

GetJson = Callable[[str], object]


class FiscalIngestionError(RuntimeError):
    pass


@dataclass(frozen=True)
class FiscalObservation:
    observation_date: str
    report_date: str
    fiscal_year: int
    receipts: float
    outlays: float
    deficit: float


class TreasuryFiscalDataClient:
    def __init__(self, get_json: GetJson | None = None, api_url: str = FISCAL_API_URL) -> None:
        self.get_json = get_json or _get_json
        self.api_url = api_url

    def build_url(self, *, start_date: str) -> str:
        query = urlencode(
            {
                "filter": f"record_date:gte:{start_date}",
                "sort": "record_date,print_order_nbr",
                "page[size]": "5000",
            }
        )
        return f"{self.api_url}?{query}"

    def fetch_observations(self, *, start_date: str) -> tuple[list[FiscalObservation], str]:
        url = self.build_url(start_date=start_date)
        payload = self.get_json(url)
        if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), list):
            raise FiscalIngestionError("Treasury Fiscal Data response did not include a data array.")
        observations = self._parse_rows(payload["data"])
        if not observations:
            raise FiscalIngestionError(f"No Monthly Treasury Statement rows found since {start_date}.")
        return observations, url

    @staticmethod
    def _parse_rows(rows: list[object]) -> list[FiscalObservation]:
        by_report: dict[str, list[Mapping[str, object]]] = {}
        for row in rows:
            if isinstance(row, Mapping):
                by_report.setdefault(str(row.get("record_date") or ""), []).append(row)

        latest_by_month: dict[str, FiscalObservation] = {}
        for report_date, report_rows in by_report.items():
            current_root = next(
                (
                    row
                    for row in report_rows
                    if str(row.get("sequence_number_cd")) == "2"
                    and str(row.get("record_type_cd")) == "SL"
                ),
                None,
            )
            if current_root is None:
                continue
            parent_id = str(current_root.get("classification_id") or "")
            fiscal_year = int(str(current_root.get("record_fiscal_year") or "0"))
            for row in report_rows:
                month = MONTHS.get(str(row.get("classification_desc") or ""))
                if (
                    month is None
                    or str(row.get("parent_id") or "") != parent_id
                    or str(row.get("record_type_cd") or "") != "MTH"
                ):
                    continue
                receipts = _float(row.get("current_month_gross_rcpt_amt"))
                outlays = _float(row.get("current_month_gross_outly_amt"))
                deficit = _float(row.get("current_month_dfct_sur_amt"))
                if receipts is None or outlays is None or deficit is None:
                    continue
                calendar_year = fiscal_year - 1 if month >= 10 else fiscal_year
                observation_date = f"{calendar_year:04d}-{month:02d}-01"
                observation = FiscalObservation(
                    observation_date, report_date, fiscal_year, receipts, outlays, deficit
                )
                previous = latest_by_month.get(observation_date)
                if previous is None or report_date >= previous.report_date:
                    latest_by_month[observation_date] = observation
        return [latest_by_month[key] for key in sorted(latest_by_month)]


class FiscalStore:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS fiscal_observations (
                    observation_date TEXT PRIMARY KEY,
                    report_date TEXT NOT NULL,
                    fiscal_year INTEGER NOT NULL,
                    receipts REAL NOT NULL,
                    outlays REAL NOT NULL,
                    deficit REAL NOT NULL,
                    fetched_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_fiscal_report_date
                    ON fiscal_observations (report_date DESC);
                CREATE TABLE IF NOT EXISTS fiscal_ingestion_runs (
                    run_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    observation_count INTEGER,
                    error_message TEXT
                );
                """
            )

    def write_observations(self, observations: list[FiscalObservation]) -> int:
        self.initialize()
        now = _now()
        with self._connection() as connection:
            connection.executemany(
                """
                INSERT OR REPLACE INTO fiscal_observations
                    (observation_date, report_date, fiscal_year, receipts, outlays, deficit, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        o.observation_date,
                        o.report_date,
                        o.fiscal_year,
                        o.receipts,
                        o.outlays,
                        o.deficit,
                        now,
                    )
                    for o in observations
                ],
            )
        return len(observations)

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
                INSERT OR REPLACE INTO fiscal_ingestion_runs
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


class FiscalIngestionService:
    def __init__(self, store: FiscalStore, client: TreasuryFiscalDataClient | None = None) -> None:
        self.store = store
        self.client = client or TreasuryFiscalDataClient()

    def ingest(self, *, start_date: str) -> dict[str, object]:
        run_id, started = str(uuid.uuid4()), _now()
        self.store.record_run(run_id=run_id, started_at=started, status="started")
        try:
            observations, source_url = self.client.fetch_observations(start_date=start_date)
            count = self.store.write_observations(observations)
            self.store.record_run(
                run_id=run_id,
                started_at=started,
                completed_at=_now(),
                status="completed",
                observation_count=count,
            )
            return {
                "run_id": run_id,
                "observation_count": count,
                "latest": observations[-1].observation_date,
                "source_url": source_url,
            }
        except Exception as exc:
            self.store.record_run(
                run_id=run_id,
                started_at=started,
                completed_at=_now(),
                status="failed",
                error_message=str(exc),
            )
            raise


def fiscal_bias(*, deficit: float, prior_deficit: float, prior_outlays: float) -> float:
    """Map YoY deficit expansion to policy bias; larger deficits are modestly hawkish."""
    if prior_outlays <= 0:
        return 0.0
    impulse = (deficit - prior_deficit) / prior_outlays
    return round(max(-1.0, min(1.0, impulse / 0.15)), 4)


def build_fiscal_bias_history(
    *, db_path: str | Path = DEFAULT_DB_PATH
) -> list[dict[str, object]]:
    try:
        connection = sqlite3.connect(db_path)
        rows = connection.execute(
            """
            SELECT observation_date, receipts, outlays, deficit
            FROM fiscal_observations ORDER BY observation_date
            """
        ).fetchall()
    except sqlite3.DatabaseError:
        return []
    finally:
        if "connection" in locals():
            connection.close()
    values = {
        str(row[0]): {"receipts": float(row[1]), "outlays": float(row[2]), "deficit": float(row[3])}
        for row in rows
    }
    history = []
    for observation_date, current in values.items():
        prior = values.get(_shift_year(observation_date))
        if prior is None:
            continue
        impulse = (current["deficit"] - prior["deficit"]) / prior["outlays"]
        history.append(
            {
                "date": observation_date,
                "bias": fiscal_bias(
                    deficit=current["deficit"],
                    prior_deficit=prior["deficit"],
                    prior_outlays=prior["outlays"],
                ),
                "deficit_billions": round(current["deficit"] / 1e9, 3),
                "deficit_yoy_change_billions": round(
                    (current["deficit"] - prior["deficit"]) / 1e9, 3
                ),
                "fiscal_impulse_ratio": round(impulse, 6),
            }
        )
    return history


def build_dashboard_fiscal_payload(
    *, db_path: str | Path = DEFAULT_DB_PATH
) -> dict[str, object] | None:
    history = build_fiscal_bias_history(db_path=db_path)
    if not history:
        return None
    latest_metric = history[-1]
    observation_date = str(latest_metric["date"])
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM fiscal_observations WHERE observation_date = ?", (observation_date,)
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    bias = float(latest_metric["bias"])
    score = _score(bias)
    metrics = {
        "receipts_billions": round(float(row["receipts"]) / 1e9, 3),
        "outlays_billions": round(float(row["outlays"]) / 1e9, 3),
        "deficit_billions": round(float(row["deficit"]) / 1e9, 3),
        "deficit_yoy_change_billions": latest_metric["deficit_yoy_change_billions"],
        "fiscal_impulse_ratio": latest_metric["fiscal_impulse_ratio"],
        "fiscal_bias": bias,
    }
    return {
        "generated_at": _now(),
        "metric_date": observation_date,
        "report_date": str(row["report_date"]),
        "provider": "U.S. Treasury Fiscal Data",
        "metrics": metrics,
        "metric_metadata": {
            "fiscal_bias": {
                "formula": "clamp(((deficit - deficit_12m_ago) / prior_outlays) / 0.15, -1, 1)",
                "note": "Deficit expansion is treated as an indirect demand/inflation impulse.",
            }
        },
        "heat_card": {
            "label": "Fiscal Impulse",
            "display": f"{bias:+.2f}",
            "score": score,
            "tone": _tone(score),
            "toneLabel": "deficit YoY",
            "sources": [
                f"MTS {row['report_date']}",
                f"deficit ${metrics['deficit_billions']:.1f}B",
                f"YoY change ${metrics['deficit_yoy_change_billions']:+.1f}B",
            ],
        },
        "latest_values": [
            {"series_id": "receipts", "label": "Federal Receipts", "value_billions": metrics["receipts_billions"]},
            {"series_id": "outlays", "label": "Federal Outlays", "value_billions": metrics["outlays_billions"]},
            {"series_id": "deficit", "label": "Federal Deficit / Surplus", "value_billions": metrics["deficit_billions"]},
        ],
        "bias_history": history,
        "source_url": FISCAL_HOME_URL,
    }


def build_fiscal_knowledge_graph_payload(
    *, db_path: str | Path = DEFAULT_DB_PATH, months: int = 12
) -> dict[str, object] | None:
    try:
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT observation_date, receipts, outlays, deficit FROM fiscal_observations
            ORDER BY observation_date DESC LIMIT ?
            """,
            (max(1, months),),
        ).fetchall()
    except sqlite3.DatabaseError:
        return None
    finally:
        if "connection" in locals():
            connection.close()
    rows = list(reversed(rows))
    if not rows:
        return None
    definitions = (
        ("receipts", "Federal Receipts", "#58a6ff"),
        ("outlays", "Federal Outlays", "#d2a8ff"),
        ("deficit", "Deficit / Surplus", "#ff9f43"),
    )
    nodes = [
        {
            "id": "fiscal-hub",
            "kind": "fiscal_hub",
            "label": "Fiscal Impulse",
            "sublabel": "Monthly Treasury Statement",
            "x": 0,
            "y": -4.2,
            "z": 3.4,
            "size": 0.24,
            "color": "#ff9f43",
        }
    ]
    edges = [{"source": "fiscal-hub", "target": "fomc-hub", "color": "#ff9f43", "opacity": 0.25}]
    for index, (key, label, color) in enumerate(definitions):
        series_id = f"fiscal-series-{key}"
        x = -1.8 + index * 1.8
        nodes.append(
            {
                "id": series_id,
                "kind": "fiscal_series",
                "label": label,
                "sublabel": "USD billions",
                "x": x,
                "y": -3.3,
                "z": 3.4,
                "size": 0.14,
                "color": color,
            }
        )
        edges.append({"source": "fiscal-hub", "target": series_id, "color": color, "opacity": 0.28})
        previous = None
        for depth, row in enumerate(rows):
            obs_id = f"fiscal-{key}-{row['observation_date']}"
            value = float(row[key]) / 1e9
            nodes.append(
                {
                    "id": obs_id,
                    "kind": "fiscal_observation",
                    "label": str(row["observation_date"]),
                    "sublabel": f"${value:.1f}B",
                    "x": x,
                    "y": round(-3 - depth * 0.25, 3),
                    "z": round(3.7 + depth * 0.25, 3),
                    "size": 0.06,
                    "color": color,
                }
            )
            edges.append({"source": series_id, "target": obs_id, "color": color, "opacity": 0.1})
            if previous:
                edges.append({"source": previous, "target": obs_id, "color": color, "opacity": 0.2})
            previous = obs_id
    return {
        "fiscal_graph": {
            "nodes": nodes,
            "edges": edges,
            "stats": {
                "series_count": len(definitions),
                "observation_count": len(definitions) * len(rows),
                "edge_count": len(edges),
            },
        }
    }


def export_dashboard_fiscal_js(
    *, db_path: str | Path = DEFAULT_DB_PATH, output_js_path: str | Path, months: int = 12
) -> dict[str, object] | None:
    payload = build_dashboard_fiscal_payload(db_path=db_path)
    output = Path(output_js_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if payload is None:
        output.write_text("window.__FISCAL_DASHBOARD_DATA__ = null;\n", encoding="utf-8")
        return None
    graph = build_fiscal_knowledge_graph_payload(db_path=db_path, months=months)
    if graph:
        payload = {**payload, **graph}
    output.write_text(
        "window.__FISCAL_DASHBOARD_DATA__ = " + json.dumps(payload, indent=2, sort_keys=True) + ";\n",
        encoding="utf-8",
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest Treasury Monthly Treasury Statement data.")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--start-date", default=f"{date.today().year - 4}-01-01")
    parser.add_argument("--dashboard-js")
    args = parser.parse_args(argv)
    result = FiscalIngestionService(FiscalStore(args.db_path)).ingest(start_date=args.start_date)
    if args.dashboard_js:
        export_dashboard_fiscal_js(db_path=args.db_path, output_js_path=args.dashboard_js)
    print(
        f"Ingested {result['observation_count']} fiscal observations "
        f"(latest={result['latest']})."
    )
    return 0


def _shift_year(value: str) -> str:
    return f"{int(value[:4]) - 1:04d}{value[4:]}"


def _score(bias: float) -> int:
    return max(1, min(5, round(3 + bias * 2)))


def _tone(score: int) -> str:
    return {1: "cold", 2: "cool", 3: "balanced", 4: "warm", 5: "hot"}[score]


def _float(value: object) -> float | None:
    try:
        text = str(value).replace(",", "")
        return None if text.lower() in {"", "null", "none"} else float(text)
    except (TypeError, ValueError):
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_json(url: str) -> object:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "WhatTheFed/1.0"})
    with urlopen(request, timeout=90) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
