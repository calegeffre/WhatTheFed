"""Import dated meeting evidence into Neo4j and export a public graph snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

from .backtest import HISTORY_GLOBALS, _period_end_or_none
from .neo4j_connection import connect, load_settings
from .prediction_history import read_js_payload


DOMAIN_LABELS = {
    "cpi": "Consumer prices",
    "ppi": "Producer prices",
    "labor": "Employment",
    "gdp": "Real GDP",
    "breakeven": "Inflation expectations",
    "treasury": "Treasury curve",
    "policyRate": "Funding conditions",
    "fiscal": "Fiscal impulse",
}
SOURCE_URLS = {
    "cpi": "https://www.bls.gov/cpi/",
    "ppi": "https://www.bls.gov/ppi/",
    "labor": "https://www.bls.gov/ces/",
    "gdp": "https://www.bea.gov/data/gdp/gross-domestic-product",
    "breakeven": "https://home.treasury.gov/resource-center/data-chart-center/interest-rates",
    "treasury": "https://home.treasury.gov/resource-center/data-chart-center/interest-rates",
    "policyRate": "https://www.newyorkfed.org/markets/reference-rates",
    "fiscal": "https://fiscaldata.treasury.gov/datasets/monthly-treasury-statement/",
}
RELATIONS = (
    "HAS_DECISION",
    "HAS_RECONSTRUCTION",
    "USES_SIGNAL",
    "DERIVED_FROM",
    "SOURCED_FROM",
)
NODE_LABELS = {
    "meeting": "WTFMeeting",
    "decision": "WTFDecision",
    "forecast": "WTFReconstruction",
    "signal": "WTFSignal",
    "observation": "WTFObservation",
    "source": "WTFSource",
}
LIMITATIONS = [
    "Availability dates are publication-lag estimates, not verified original release timestamps.",
    "Historical values can include later revisions. This is not an unrevised-vintage archive.",
    "Forecast nodes are retrospective model reconstructions, not forecasts captured at the time.",
    "Connections describe model provenance, not causation or evidence of the Fed's actual reasoning.",
    "Historical market prices, speeches, and individual votes are not included in this first version.",
]


class MeetingEvidenceUnavailable(ValueError):
    """Raised when no completed backtest meetings are available for the explorer."""


def build_evidence_graph(
    data_dir: Path,
    *,
    as_of: date | None = None,
) -> dict[str, object]:
    backtest = read_js_payload(data_dir / "data_backtest_data.js", "__DATA_BACKTEST_DATA__")
    cutoff = as_of or date.today()
    results = sorted(
        (item for item in backtest["meetings"] if date.fromisoformat(item["meeting_date"]) <= cutoff),
        key=lambda item: item["meeting_date"],
    )
    if not results:
        raise MeetingEvidenceUnavailable(
            "No completed meetings have evidence to import. Rebuild the Fed rate history "
            "and backtest with refresh_fed_history and refresh_backtest in a manual workflow run."
        )
    history = read_js_payload(data_dir / "fed_rate_history_data.js", "__FED_RATE_HISTORY_DATA__")
    targets = {item["meeting_date"]: item for item in history["meetings"]}
    observations = {}
    for domain, (filename, global_name, history_key, _) in HISTORY_GLOBALS.items():
        payload = read_js_payload(data_dir / filename, global_name)
        observations[domain] = {str(item["date"]): item for item in payload[history_key]}

    nodes: dict[str, dict[str, object]] = {}
    edges: dict[tuple[str, str, str], dict[str, str]] = {}
    meetings = []

    def node(node_id: str, kind: str, label: str, **properties: object) -> str:
        nodes[node_id] = {"id": node_id, "kind": kind, "label": label, **properties}
        return node_id

    def edge(source: str, target: str, relation: str) -> None:
        edges[(source, target, relation)] = {
            "source": source, "target": target, "relation": relation,
        }

    for result in results:
        meeting_date = date.fromisoformat(result["meeting_date"])
        target = targets[meeting_date.isoformat()]
        meeting_id = node(
            f"meeting:{meeting_date}", "meeting", meeting_date.isoformat(),
            meeting_date=meeting_date.isoformat(),
            source_url=target["source_url"],
            details="Completed FOMC meeting. The decision is the outcome, not pre-meeting evidence.",
        )
        decision_id = node(
            f"decision:{meeting_date}", "decision", str(result["actual"]).upper(),
            meeting_date=meeting_date.isoformat(),
            target_lower=target["target_lower"],
            target_upper=target["target_upper"],
            source_url=target["source_url"],
            details="Recorded target-rate outcome.",
        )
        edge(meeting_id, decision_id, "HAS_DECISION")
        forecast_id = node(
            f"reconstruction:{meeting_date}", "forecast",
            f"Model: {str(result['predicted']).upper()}",
            meeting_date=meeting_date.isoformat(),
            model_version=backtest["model_version"],
            confidence=result["confidence"],
            details="Retrospective reconstruction; not a historical live forecast.",
            probabilities=json.dumps(result["probabilities"], sort_keys=True),
        )
        edge(meeting_id, forecast_id, "HAS_RECONSTRUCTION")
        for domain, observation_date in result["input_dates"].items():
            observation = observations[domain][observation_date]
            period_end = _period_end_or_none(
                observation_date,
                monthly_period=domain in {"cpi", "ppi", "labor"},
            )
            if period_end is None:
                raise ValueError(f"Invalid observation period for {domain}: {observation_date}.")
            available_on = period_end + timedelta(days=HISTORY_GLOBALS[domain][3])
            if available_on >= meeting_date:
                raise ValueError(
                    f"{domain} observation {observation_date} is not eligible before {meeting_date}."
                )
            bias = float(observation["bias"])
            if not math.isfinite(bias):
                raise ValueError(f"Non-finite evidence bias for {domain} at {observation_date}.")
            signal_id = node(
                f"signal:{meeting_date}:{domain}", "signal", DOMAIN_LABELS[domain],
                domain=domain, bias=bias,
                meeting_date=meeting_date.isoformat(),
                details="Normalized model input: -1 cut pressure, 0 neutral, +1 raise pressure.",
            )
            observation_id = node(
                f"observation:{domain}:{observation_date}", "observation",
                f"{observation_date} | {bias:+.2f}",
                domain=domain, bias=bias, observation_period=observation_date,
                estimated_available_on=available_on.isoformat(),
                details=json.dumps(observation, sort_keys=True, allow_nan=False),
            )
            source_id = node(
                f"source:{domain}", "source", f"{DOMAIN_LABELS[domain]} source",
                domain=domain, source_url=SOURCE_URLS[domain],
                details="Official source landing page, not an archived vintage release.",
            )
            edge(forecast_id, signal_id, "USES_SIGNAL")
            edge(signal_id, observation_id, "DERIVED_FROM")
            edge(observation_id, source_id, "SOURCED_FROM")
        meetings.append({
            "id": meeting_id,
            "meeting_date": meeting_date.isoformat(),
            "actual": result["actual"],
            "predicted": result["predicted"],
        })

    return {
        "meetings": meetings,
        "nodes": sorted(nodes.values(), key=lambda item: item["id"]),
        "edges": sorted(edges.values(), key=lambda item: (item["source"], item["target"], item["relation"])),
    }


def sync_and_read_graph(driver, database: str, graph: Mapping[str, object]) -> dict[str, object]:
    snapshot_id = hashlib.sha256(
        json.dumps(graph, sort_keys=True, allow_nan=False).encode("utf-8")
    ).hexdigest()
    with driver.session(database=database) as session:
        session.run(
            "CREATE CONSTRAINT whatthefed_evidence_identity IF NOT EXISTS "
            "FOR (n:WTFMeetingEvidence) REQUIRE (n.snapshot_id, n.id) IS UNIQUE"
        ).consume()

        def write_graph(tx):
            tx.run(
                "UNWIND $nodes AS properties "
                "MERGE (n:WTFMeetingEvidence {snapshot_id: $snapshot_id, id: properties.id}) "
                "SET n += properties",
                snapshot_id=snapshot_id, nodes=graph["nodes"],
            ).consume()
            for kind, label in NODE_LABELS.items():
                tx.run(
                    "MATCH (n:WTFMeetingEvidence {snapshot_id: $snapshot_id, kind: $kind}) "
                    f"SET n:{label}",
                    snapshot_id=snapshot_id, kind=kind,
                ).consume()
            for relation in RELATIONS:
                rows = [item for item in graph["edges"] if item["relation"] == relation]
                tx.run(
                    "UNWIND $edges AS edge "
                    "MATCH (a:WTFMeetingEvidence {snapshot_id: $snapshot_id, id: edge.source}) "
                    "MATCH (b:WTFMeetingEvidence {snapshot_id: $snapshot_id, id: edge.target}) "
                    f"MERGE (a)-[:{relation}]->(b)",
                    snapshot_id=snapshot_id, edges=rows,
                ).consume()

        session.execute_write(write_graph)
        nodes = session.run(
            "MATCH (n:WTFMeetingEvidence {snapshot_id: $snapshot_id}) "
            "RETURN properties(n) AS properties ORDER BY n.id",
            snapshot_id=snapshot_id,
        ).data()
        edges = session.run(
            "MATCH (a:WTFMeetingEvidence {snapshot_id: $snapshot_id})-[r]->"
            "(b:WTFMeetingEvidence {snapshot_id: $snapshot_id}) "
            "RETURN a.id AS source, b.id AS target, type(r) AS relation "
            "ORDER BY source, target, relation",
            snapshot_id=snapshot_id,
        ).data()
    public_nodes = [
        {key: value for key, value in row["properties"].items() if key != "snapshot_id"}
        for row in nodes
    ]
    if public_nodes != graph["nodes"] or edges != graph["edges"]:
        raise RuntimeError("Neo4j round-trip verification failed; refusing to export incomplete evidence.")
    return {
        "available": True,
        "backend": "Neo4j",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_id": snapshot_id,
        "limitations": LIMITATIONS,
        "meetings": graph["meetings"],
        "nodes": public_nodes,
        "edges": edges,
    }


def export_evidence_js(payload: Mapping[str, object], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "window.__MEETING_EVIDENCE_DATA__ = "
        + json.dumps(payload, sort_keys=True, indent=2, allow_nan=False)
        + ";\n",
        encoding="utf-8",
    )


def build_neo4j_evidence(data_dir: Path, settings: dict[str, str]) -> dict[str, object]:
    graph = build_evidence_graph(data_dir)
    with connect(settings) as driver:
        driver.verify_connectivity()
        return sync_and_read_graph(driver, settings["NEO4J_DATABASE"], graph)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--output-js", type=Path, required=True)
    args = parser.parse_args(argv)
    payload = build_neo4j_evidence(args.data_dir, load_settings(args.env_file))
    export_evidence_js(payload, args.output_js)
    print(f"Exported verified Neo4j evidence for {len(payload['meetings'])} meetings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
