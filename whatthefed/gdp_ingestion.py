"""Quarterly GDP ingestion from BEA's official NIPA workbook."""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sqlite3
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Callable, Iterable
from urllib.request import Request, urlopen
from xml.etree import ElementTree


DEFAULT_DB_PATH = Path("data") / "market_snapshots.db"
BEA_GDP_PAGE_URL = "https://www.bea.gov/data/gdp/gross-domestic-product"
BEA_SECTION1_WORKBOOK_URL = (
    "https://apps.bea.gov/national/Release/XLS/Survey/Section1All_xls.xlsx"
)
QUARTER_RE = re.compile(r"^\d{4}Q[1-4]$")

XML_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}


@dataclass(frozen=True)
class GDPSeriesDefinition:
    series_id: str
    label: str
    category: str
    display_order: int
    color: str


@dataclass(frozen=True)
class GDPObservation:
    series_id: str
    period: str
    annualized_growth_pct: float


GDP_SERIES_DEFINITIONS: tuple[GDPSeriesDefinition, ...] = (
    GDPSeriesDefinition("A191RL", "Real Gross Domestic Product", "headline", 1, "#56d4dd"),
    GDPSeriesDefinition("DPCERL", "Personal Consumption Expenditures", "consumption", 2, "#7bd389"),
    GDPSeriesDefinition("A006RL", "Gross Private Domestic Investment", "investment", 3, "#f2cc60"),
    GDPSeriesDefinition("A007RL", "Fixed Investment", "investment", 4, "#ff9f43"),
    GDPSeriesDefinition("A020RL", "Exports", "trade", 5, "#58a6ff"),
    GDPSeriesDefinition("A021RL", "Imports", "trade", 6, "#d2a8ff"),
    GDPSeriesDefinition("A822RL", "Government Consumption and Investment", "government", 7, "#f97583"),
)

GetBytes = Callable[[str], bytes]


class GDPIngestionError(RuntimeError):
    pass


class BEAQuarterlyGDPClient:
    def __init__(
        self,
        get_bytes: GetBytes | None = None,
        workbook_url: str = BEA_SECTION1_WORKBOOK_URL,
    ) -> None:
        self.get_bytes = get_bytes or _get_bytes
        self.workbook_url = workbook_url

    def fetch_observations(
        self,
        *,
        series_ids: Iterable[str],
        start_year: int | None = None,
    ) -> tuple[list[GDPObservation], str]:
        rows = _read_xlsx_sheet(self.get_bytes(self.workbook_url), "T10101-Q")
        if len(rows) < 2:
            raise GDPIngestionError("BEA workbook did not contain quarterly GDP rows.")
        header = rows[0]
        periods = {column: value for column, value in header.items() if QUARTER_RE.match(value)}
        selected = set(series_ids)
        observations: list[GDPObservation] = []
        for row in rows[1:]:
            series_id = row.get(3, "").strip()
            if series_id not in selected:
                continue
            for column, period in periods.items():
                if start_year is not None and int(period[:4]) < start_year:
                    continue
                value = _float(row.get(column))
                if value is not None:
                    observations.append(GDPObservation(series_id, period, value))
        if not observations:
            raise GDPIngestionError("BEA workbook contained no selected quarterly GDP observations.")
        observations.sort(key=lambda item: (item.period, item.series_id))
        return observations, self.workbook_url


class GDPStore:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS gdp_series_catalog (
                    series_id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    category TEXT NOT NULL,
                    display_order INTEGER NOT NULL,
                    color TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS gdp_observations (
                    series_id TEXT NOT NULL,
                    period TEXT NOT NULL,
                    annualized_growth_pct REAL NOT NULL,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (series_id, period)
                );
                CREATE INDEX IF NOT EXISTS idx_gdp_period ON gdp_observations (period DESC);
                CREATE TABLE IF NOT EXISTS gdp_ingestion_runs (
                    run_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    observation_count INTEGER,
                    error_message TEXT
                );
                """
            )

    def write_catalog(self, definitions: Iterable[GDPSeriesDefinition]) -> None:
        self.initialize()
        now = _now()
        with self._connection() as connection:
            connection.executemany(
                """
                INSERT OR REPLACE INTO gdp_series_catalog
                    (series_id, label, category, display_order, color, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [(d.series_id, d.label, d.category, d.display_order, d.color, now) for d in definitions],
            )

    def write_observations(self, observations: Iterable[GDPObservation]) -> int:
        self.initialize()
        values = list(observations)
        now = _now()
        with self._connection() as connection:
            connection.executemany(
                """
                INSERT OR REPLACE INTO gdp_observations
                    (series_id, period, annualized_growth_pct, fetched_at)
                VALUES (?, ?, ?, ?)
                """,
                [(o.series_id, o.period, o.annualized_growth_pct, now) for o in values],
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
                INSERT OR REPLACE INTO gdp_ingestion_runs
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


class GDPIngestionService:
    def __init__(
        self,
        store: GDPStore,
        client: BEAQuarterlyGDPClient | None = None,
        definitions: Iterable[GDPSeriesDefinition] = GDP_SERIES_DEFINITIONS,
    ) -> None:
        self.store = store
        self.client = client or BEAQuarterlyGDPClient()
        self.definitions = tuple(definitions)

    def ingest(self, *, start_year: int | None = None) -> dict[str, object]:
        run_id, started = str(uuid.uuid4()), _now()
        self.store.record_run(run_id=run_id, started_at=started, status="started")
        try:
            self.store.write_catalog(self.definitions)
            observations, source_url = self.client.fetch_observations(
                series_ids=[item.series_id for item in self.definitions],
                start_year=start_year,
            )
            count = self.store.write_observations(observations)
            latest = max(item.period for item in observations)
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
                "latest_period": latest,
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


def gdp_bias(real_gdp_growth_pct: float | None) -> float:
    """Map annualized real GDP growth onto the shared policy-bias scale."""
    if real_gdp_growth_pct is None:
        return 0.0
    return round(max(-1.0, min(1.0, (real_gdp_growth_pct - 2.0) / 3.0)), 4)


def build_gdp_bias_history(
    *, db_path: str | Path = DEFAULT_DB_PATH, headline_id: str = "A191RL"
) -> list[dict[str, object]]:
    try:
        connection = sqlite3.connect(db_path)
        rows = connection.execute(
            """
            SELECT period, annualized_growth_pct FROM gdp_observations
            WHERE series_id = ? ORDER BY period
            """,
            (headline_id,),
        ).fetchall()
    except sqlite3.DatabaseError:
        return []
    finally:
        if "connection" in locals():
            connection.close()
    return [
        {"date": str(period), "bias": gdp_bias(float(value)), "real_gdp_growth_pct": float(value)}
        for period, value in rows
    ]


def build_dashboard_gdp_payload(
    *, db_path: str | Path = DEFAULT_DB_PATH
) -> dict[str, object] | None:
    history = build_gdp_bias_history(db_path=db_path)
    if not history:
        return None
    latest = history[-1]
    period = str(latest["date"])
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT o.series_id, o.annualized_growth_pct, c.label, c.category
            FROM gdp_observations o JOIN gdp_series_catalog c USING(series_id)
            WHERE o.period = ? ORDER BY c.display_order
            """,
            (period,),
        ).fetchall()
    finally:
        connection.close()
    bias = float(latest["bias"])
    score = _score(bias)
    metrics = {
        "real_gdp_growth_pct": float(latest["real_gdp_growth_pct"]),
        "gdp_bias": bias,
    }
    return {
        "generated_at": _now(),
        "metric_date": period,
        "provider": "Bureau of Economic Analysis",
        "metrics": metrics,
        "metric_metadata": {
            "gdp_bias": {
                "formula": "clamp((real_gdp_annualized_growth - 2.0) / 3.0, -1, 1)",
                "note": "Above-trend growth is hawkish; contraction or weak growth is dovish.",
            }
        },
        "heat_card": {
            "label": "Real GDP Growth",
            "display": f"{metrics['real_gdp_growth_pct']:.1f}%",
            "score": score,
            "tone": _tone(score),
            "toneLabel": f"bias {bias:+.2f}",
            "sources": [f"BEA {period}", "quarterly SAAR", f"bias {bias:+.2f}"],
        },
        "latest_values": [
            {
                "series_id": str(row["series_id"]),
                "label": str(row["label"]),
                "category": str(row["category"]),
                "annualized_growth_pct": float(row["annualized_growth_pct"]),
            }
            for row in rows
        ],
        "bias_history": history,
        "source_url": BEA_GDP_PAGE_URL,
        "download_url": BEA_SECTION1_WORKBOOK_URL,
    }


def build_gdp_knowledge_graph_payload(
    *, db_path: str | Path = DEFAULT_DB_PATH, per_series_limit: int = 8
) -> dict[str, object] | None:
    try:
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        series = connection.execute(
            "SELECT * FROM gdp_series_catalog ORDER BY display_order"
        ).fetchall()
        observations = connection.execute(
            """
            WITH ranked AS (
              SELECT *, ROW_NUMBER() OVER(PARTITION BY series_id ORDER BY period DESC) rank
              FROM gdp_observations
            )
            SELECT series_id, period, annualized_growth_pct FROM ranked WHERE rank <= ?
            ORDER BY series_id, period
            """,
            (max(1, per_series_limit),),
        ).fetchall()
    except sqlite3.DatabaseError:
        return None
    finally:
        if "connection" in locals():
            connection.close()
    if not series or not observations:
        return None
    nodes = [
        {
            "id": "gdp-hub",
            "kind": "gdp_hub",
            "label": "Economic Growth",
            "sublabel": "BEA real GDP",
            "x": 0,
            "y": 5.1,
            "z": 0,
            "size": 0.26,
            "color": "#56d4dd",
        }
    ]
    edges = [{"source": "gdp-hub", "target": "fomc-hub", "color": "#56d4dd", "opacity": 0.3}]
    by_series: dict[str, list[sqlite3.Row]] = {}
    for row in observations:
        by_series.setdefault(str(row["series_id"]), []).append(row)
    count = 0
    for index, row in enumerate(series):
        series_id = str(row["series_id"])
        node_id = f"gdp-series-{series_id.lower()}"
        x = -4.5 + index * 1.5
        color = str(row["color"])
        nodes.append(
            {
                "id": node_id,
                "kind": "gdp_series",
                "label": str(row["label"]),
                "sublabel": series_id,
                "x": x,
                "y": 4.2,
                "z": 0,
                "size": 0.14,
                "color": color,
            }
        )
        edges.append({"source": "gdp-hub", "target": node_id, "color": color, "opacity": 0.28})
        previous = None
        for depth, observation in enumerate(by_series.get(series_id, [])):
            obs_id = f"gdp-{series_id.lower()}-{observation['period']}"
            count += 1
            nodes.append(
                {
                    "id": obs_id,
                    "kind": "gdp_observation",
                    "label": str(observation["period"]),
                    "sublabel": f"{float(observation['annualized_growth_pct']):.1f}%",
                    "x": x,
                    "y": round(3.9 - depth * 0.3, 3),
                    "z": round(0.3 + depth * 0.3, 3),
                    "size": 0.06,
                    "color": color,
                }
            )
            edges.append({"source": node_id, "target": obs_id, "color": color, "opacity": 0.1})
            if previous:
                edges.append({"source": previous, "target": obs_id, "color": color, "opacity": 0.2})
            previous = obs_id
    return {
        "gdp_graph": {
            "nodes": nodes,
            "edges": edges,
            "stats": {"series_count": len(series), "observation_count": count, "edge_count": len(edges)},
        }
    }


def export_dashboard_gdp_js(
    *, db_path: str | Path = DEFAULT_DB_PATH, output_js_path: str | Path, per_series_limit: int = 8
) -> dict[str, object] | None:
    payload = build_dashboard_gdp_payload(db_path=db_path)
    output = Path(output_js_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if payload is None:
        output.write_text("window.__GDP_DASHBOARD_DATA__ = null;\n", encoding="utf-8")
        return None
    graph = build_gdp_knowledge_graph_payload(
        db_path=db_path, per_series_limit=per_series_limit
    )
    if graph:
        payload = {**payload, **graph}
    output.write_text(
        "window.__GDP_DASHBOARD_DATA__ = " + json.dumps(payload, indent=2, sort_keys=True) + ";\n",
        encoding="utf-8",
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest quarterly real GDP growth from BEA.")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--start-year", type=int, default=datetime.now().year - 12)
    parser.add_argument("--dashboard-js")
    args = parser.parse_args(argv)
    result = GDPIngestionService(GDPStore(args.db_path)).ingest(start_year=args.start_year)
    if args.dashboard_js:
        export_dashboard_gdp_js(db_path=args.db_path, output_js_path=args.dashboard_js)
    print(
        f"Ingested {result['observation_count']} quarterly GDP observations "
        f"(latest={result['latest_period']})."
    )
    return 0


def _read_xlsx_sheet(workbook_bytes: bytes, sheet_name: str) -> list[dict[int, str]]:
    try:
        archive = zipfile.ZipFile(BytesIO(workbook_bytes))
    except zipfile.BadZipFile as exc:
        raise GDPIngestionError("BEA GDP download was not a valid XLSX workbook.") from exc
    with archive:
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        relationships = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {
            item.attrib["Id"]: item.attrib["Target"]
            for item in relationships.findall("r:Relationship", PACKAGE_REL_NS)
        }
        sheet = next(
            (
                item
                for item in workbook.findall(".//m:sheet", XML_NS)
                if item.attrib.get("name") == sheet_name
            ),
            None,
        )
        if sheet is None:
            raise GDPIngestionError(f"BEA workbook did not contain sheet {sheet_name}.")
        relationship_id = sheet.attrib[f"{{{REL_NS}}}id"]
        target = targets[relationship_id].lstrip("/")
        if not target.startswith("xl/"):
            target = f"xl/{target}"

        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = [
                "".join(node.text or "" for node in item.findall(".//m:t", XML_NS))
                for item in root.findall("m:si", XML_NS)
            ]

        root = ElementTree.fromstring(archive.read(target))
        rows: list[dict[int, str]] = []
        for row in root.findall(".//m:sheetData/m:row", XML_NS):
            row_number = int(row.attrib.get("r", "0"))
            if row_number < 8:
                continue
            values: dict[int, str] = {}
            for cell in row.findall("m:c", XML_NS):
                reference = cell.attrib.get("r", "")
                column = _column_number(reference)
                value_node = cell.find("m:v", XML_NS)
                value = "" if value_node is None else str(value_node.text or "")
                if cell.attrib.get("t") == "s" and value:
                    value = shared[int(value)]
                elif cell.attrib.get("t") == "inlineStr":
                    value = "".join(
                        node.text or "" for node in cell.findall(".//m:t", XML_NS)
                    )
                values[column] = value
            rows.append(values)
        return rows


def _column_number(reference: str) -> int:
    letters = "".join(character for character in reference if character.isalpha())
    result = 0
    for character in letters.upper():
        result = result * 26 + ord(character) - ord("A") + 1
    return result


def _float(value: object) -> float | None:
    try:
        text = str(value).strip().replace(",", "")
        return None if not text or text == "....." else float(text)
    except (TypeError, ValueError):
        return None


def _score(bias: float) -> int:
    return max(1, min(5, round(3 + bias * 2)))


def _tone(score: int) -> str:
    return {1: "cold", 2: "cool", 3: "balanced", 4: "warm", 5: "hot"}[score]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_bytes(url: str) -> bytes:
    request = Request(url, headers={"Accept": "*/*", "User-Agent": "WhatTheFed/1.0"})
    with urlopen(request, timeout=120) as response:
        return response.read()


if __name__ == "__main__":
    raise SystemExit(main())
