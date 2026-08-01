"""Ingestion of Federal Reserve Bank of New York reference rates.

EFFR is the rate the FOMC actually targets, so this feed anchors the dashboard to
the live policy setting instead of a hardcoded assumption. The secured rates
(SOFR/BGCR/TGCR) alongside the EFFR percentile spread expose money-market funding
stress, which historically argues against further tightening.
"""

from __future__ import annotations

import argparse
import contextlib
import math
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
PROVIDER = "NY Fed"
NYFED_BASE_URL = "https://markets.newyorkfed.org/api/rates"
NYFED_HOME_URL = "https://www.newyorkfed.org/markets/reference-rates"

# rate type -> (family, label, description)
RATE_TYPES: dict[str, tuple[str, str, str]] = {
    "EFFR": ("unsecured", "Effective Fed Funds Rate", "The rate the FOMC sets its target band around."),
    "OBFR": ("unsecured", "Overnight Bank Funding Rate", "Broader unsecured overnight bank funding."),
    "SOFR": ("secured", "Secured Overnight Financing Rate", "Treasury repo benchmark rate."),
    "BGCR": ("secured", "Broad General Collateral Rate", "General collateral repo financing."),
    "TGCR": ("secured", "Tri-Party General Collateral Rate", "Tri-party general collateral repo."),
}

# Neutral anchors for the funding-stress signal, in basis points unless noted.
NEUTRAL_SOFR_EFFR_SPREAD_BPS = 0.0
STRESS_SOFR_EFFR_SPREAD_BPS = 15.0
NEUTRAL_REPO_DISPERSION_BPS = 10.0
STRESS_REPO_DISPERSION_BPS = 25.0
NEUTRAL_BAND_POSITION = 0.5
STRESS_BAND_POSITION_SPAN = 0.35


class PolicyRateIngestionError(RuntimeError):
    pass


GetJson = Callable[[str], object]


@dataclass(frozen=True)
class PolicyRatePoint:
    effective_date: str
    rate_type: str
    percent_rate: float | None
    percentile_1: float | None
    percentile_25: float | None
    percentile_75: float | None
    percentile_99: float | None
    target_rate_from: float | None
    target_rate_to: float | None
    volume_billions: float | None
    source_url: str
    provider: str = PROVIDER


class NyFedReferenceRateClient:
    """Reads the NY Fed reference rates API (public, unauthenticated, JSON)."""

    def __init__(
        self,
        *,
        get_json: GetJson | None = None,
        base_url: str = NYFED_BASE_URL,
    ) -> None:
        self.get_json = get_json or _get_json
        self.base_url = base_url

    def build_url(self, rate_type: str, *, start_date: str, end_date: str) -> str:
        family = RATE_TYPES[rate_type][0]
        return (
            f"{self.base_url}/{family}/{rate_type.lower()}/search.json"
            f"?startDate={start_date}&endDate={end_date}"
        )

    def fetch_points(
        self,
        *,
        start_date: str,
        end_date: str,
        rate_types: Iterable[str] | None = None,
    ) -> tuple[str, list[PolicyRatePoint], str]:
        selected = list(rate_types or RATE_TYPES.keys())
        points: list[PolicyRatePoint] = []
        source_url = NYFED_HOME_URL
        for rate_type in selected:
            if rate_type not in RATE_TYPES:
                raise PolicyRateIngestionError(f"Unknown NY Fed rate type: {rate_type}")
            url = self.build_url(rate_type, start_date=start_date, end_date=end_date)
            payload = self.get_json(url)
            points.extend(self._parse_payload(payload, rate_type, url))
            source_url = url

        if not points:
            raise PolicyRateIngestionError(
                f"NY Fed reference rate feed returned no rows between {start_date} and {end_date}."
            )
        points.sort(key=lambda item: (item.effective_date, item.rate_type))
        latest = max(point.effective_date for point in points)
        return latest, points, source_url

    def _parse_payload(self, payload: object, rate_type: str, source_url: str) -> list[PolicyRatePoint]:
        if not isinstance(payload, Mapping):
            raise PolicyRateIngestionError("NY Fed response was not a JSON object.")
        rows = payload.get("refRates")
        if not isinstance(rows, list):
            raise PolicyRateIngestionError("NY Fed response did not include a refRates array.")

        points: list[PolicyRatePoint] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            effective_date = str(row.get("effectiveDate") or "").strip()
            if not effective_date:
                continue
            # The API echoes the requested type, but trust the row when present.
            row_type = str(row.get("type") or rate_type).strip().upper()
            if row_type not in RATE_TYPES:
                continue
            points.append(
                PolicyRatePoint(
                    effective_date=effective_date,
                    rate_type=row_type,
                    percent_rate=_coerce_float(row.get("percentRate")),
                    percentile_1=_coerce_float(row.get("percentPercentile1")),
                    percentile_25=_coerce_float(row.get("percentPercentile25")),
                    percentile_75=_coerce_float(row.get("percentPercentile75")),
                    percentile_99=_coerce_float(row.get("percentPercentile99")),
                    target_rate_from=_coerce_float(row.get("targetRateFrom")),
                    target_rate_to=_coerce_float(row.get("targetRateTo")),
                    volume_billions=_coerce_float(row.get("volumeInBillions")),
                    source_url=source_url,
                )
            )
        return points


class PolicyRateStore:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._open_connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS policy_rate_observations (
                    effective_date TEXT NOT NULL,
                    rate_type TEXT NOT NULL,
                    percent_rate REAL,
                    percentile_1 REAL,
                    percentile_25 REAL,
                    percentile_75 REAL,
                    percentile_99 REAL,
                    target_rate_from REAL,
                    target_rate_to REAL,
                    volume_billions REAL,
                    source_url TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (effective_date, rate_type)
                );
                CREATE INDEX IF NOT EXISTS idx_policy_rate_date
                ON policy_rate_observations (effective_date DESC);

                CREATE TABLE IF NOT EXISTS policy_rate_ingestion_runs (
                    run_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    observation_count INTEGER,
                    effective_date TEXT,
                    source_url TEXT,
                    error_message TEXT
                );
                """
            )

    def write_observations(self, observations: Iterable[PolicyRatePoint]) -> int:
        self.initialize()
        values = list(observations)
        if not values:
            return 0
        fetched_at = _utcnow().isoformat()
        with self._open_connection() as connection:
            connection.executemany(
                """
                INSERT OR REPLACE INTO policy_rate_observations (
                    effective_date, rate_type, percent_rate, percentile_1, percentile_25,
                    percentile_75, percentile_99, target_rate_from, target_rate_to,
                    volume_billions, source_url, provider, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.effective_date,
                        item.rate_type,
                        item.percent_rate,
                        item.percentile_1,
                        item.percentile_25,
                        item.percentile_75,
                        item.percentile_99,
                        item.target_rate_from,
                        item.target_rate_to,
                        item.volume_billions,
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
        effective_date: str | None = None,
        source_url: str | None = None,
        error_message: str | None = None,
    ) -> None:
        self.initialize()
        with self._open_connection() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO policy_rate_ingestion_runs (
                    run_id, started_at, completed_at, status, observation_count,
                    effective_date, source_url, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    started_at,
                    completed_at,
                    status,
                    observation_count,
                    effective_date,
                    source_url,
                    error_message,
                ),
            )

    def load_documents(self, *, per_type_limit: int = 12) -> list[Document]:
        self.initialize()
        with self._open_connection() as connection:
            rows = connection.execute(
                """
                WITH ranked AS (
                    SELECT
                        rate_type, effective_date, percent_rate, percentile_1, percentile_99,
                        target_rate_from, target_rate_to, volume_billions, source_url,
                        ROW_NUMBER() OVER (
                            PARTITION BY rate_type ORDER BY effective_date DESC
                        ) AS row_rank
                    FROM policy_rate_observations
                )
                SELECT rate_type, effective_date, percent_rate, percentile_1, percentile_99,
                       target_rate_from, target_rate_to, volume_billions, source_url
                FROM ranked
                WHERE row_rank <= ?
                ORDER BY rate_type ASC, effective_date DESC
                """,
                (per_type_limit,),
            ).fetchall()

        docs: list[Document] = []
        for row in rows:
            rate_type = str(row["rate_type"])
            effective_date = str(row["effective_date"])
            label = RATE_TYPES.get(rate_type, ("", rate_type, ""))[1]
            rate = _coerce_float(row["percent_rate"])
            rate_str = f"{rate:.3f}%" if rate is not None else "n/a"
            band_from = _coerce_float(row["target_rate_from"])
            band_to = _coerce_float(row["target_rate_to"])
            band_str = (
                f" within an FOMC target band of {band_from:.2f}%-{band_to:.2f}%"
                if band_from is not None and band_to is not None
                else ""
            )
            volume = _coerce_float(row["volume_billions"])
            volume_str = f"{volume:.0f}B" if volume is not None else "n/a"
            docs.append(
                Document(
                    source=f"policy_rate_{rate_type}_{effective_date.replace('-', '')}",
                    content=(
                        f"{label} ({rate_type}) on {effective_date}: {rate_str}{band_str}, "
                        f"volume {volume_str}."
                    ),
                    kind="policy_rate_observation",
                    published_at=effective_date,
                    source_url=str(row["source_url"]),
                    metadata={
                        "series_id": rate_type,
                        "series_label": label,
                        "category": "policy_rate",
                        "observation_date": effective_date,
                        "value": rate,
                        "target_rate_from": band_from,
                        "target_rate_to": band_to,
                        "volume_billions": volume,
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


class PolicyRateIngestionService:
    def __init__(self, *, store: PolicyRateStore, client: NyFedReferenceRateClient | None = None) -> None:
        self.store = store
        self.client = client or NyFedReferenceRateClient()

    def ingest(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        rate_types: Iterable[str] | None = None,
    ) -> dict[str, object]:
        run_id = str(uuid.uuid4())
        started_at = _utcnow().isoformat()
        self.store.record_run(run_id=run_id, started_at=started_at, status="started")
        resolved_end = end_date or date.today().isoformat()
        resolved_start = start_date or f"{date.today().year - 2}-01-01"
        try:
            latest, points, source_url = self.client.fetch_points(
                start_date=resolved_start, end_date=resolved_end, rate_types=rate_types
            )
            observation_count = self.store.write_observations(points)
            self.store.record_run(
                run_id=run_id,
                started_at=started_at,
                completed_at=_utcnow().isoformat(),
                status="completed",
                observation_count=observation_count,
                effective_date=latest,
                source_url=source_url,
            )
            return {
                "run_id": run_id,
                "observation_count": observation_count,
                "effective_date": latest,
                "source_url": source_url,
                "start_date": resolved_start,
                "end_date": resolved_end,
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


def policy_rate_bias(
    *,
    sofr_effr_spread_bps: float | None,
    repo_dispersion_bps: float | None,
    band_position: float | None,
) -> float:
    """Map money-market funding stress onto the shared [-1, +1] bias scale.

    Funding stress is a *dovish* signal: when overnight markets strain, the
    committee has historically paused or eased rather than tightened further, so
    the stress score is negated before it is returned.
    """
    spread_component = _clamp(
        ((sofr_effr_spread_bps if sofr_effr_spread_bps is not None else NEUTRAL_SOFR_EFFR_SPREAD_BPS)
         - NEUTRAL_SOFR_EFFR_SPREAD_BPS)
        / (STRESS_SOFR_EFFR_SPREAD_BPS - NEUTRAL_SOFR_EFFR_SPREAD_BPS),
        -1.0,
        1.0,
    )
    dispersion_component = _clamp(
        ((repo_dispersion_bps if repo_dispersion_bps is not None else NEUTRAL_REPO_DISPERSION_BPS)
         - NEUTRAL_REPO_DISPERSION_BPS)
        / (STRESS_REPO_DISPERSION_BPS - NEUTRAL_REPO_DISPERSION_BPS),
        -1.0,
        1.0,
    )
    band_component = _clamp(
        ((band_position if band_position is not None else NEUTRAL_BAND_POSITION) - NEUTRAL_BAND_POSITION)
        / STRESS_BAND_POSITION_SPAN,
        -1.0,
        1.0,
    )
    stress = (0.45 * spread_component) + (0.30 * dispersion_component) + (0.25 * band_component)
    return round(_clamp(-stress, -1.0, 1.0), 4)


def _metrics_for_date(rates: Mapping[str, Mapping[str, float | None]]) -> dict[str, float] | None:
    effr = rates.get("EFFR")
    sofr = rates.get("SOFR")
    if not effr or effr.get("percent_rate") is None:
        return None

    effr_rate = effr["percent_rate"]
    metrics: dict[str, float] = {"effr": float(effr_rate)}

    band_from = effr.get("target_rate_from")
    band_to = effr.get("target_rate_to")
    band_position = None
    if band_from is not None and band_to is not None and band_to > band_from:
        band_position = (float(effr_rate) - float(band_from)) / (float(band_to) - float(band_from))
        metrics["target_rate_from"] = float(band_from)
        metrics["target_rate_to"] = float(band_to)
        metrics["target_rate_mid"] = round((float(band_from) + float(band_to)) / 2.0, 4)
        metrics["effr_band_position"] = round(band_position, 4)

    sofr_effr_spread_bps = None
    if sofr and sofr.get("percent_rate") is not None:
        metrics["sofr"] = float(sofr["percent_rate"])
        sofr_effr_spread_bps = (float(sofr["percent_rate"]) - float(effr_rate)) * 100.0
        metrics["sofr_effr_spread_bps"] = round(sofr_effr_spread_bps, 2)

    repo_dispersion_bps = None
    if sofr and sofr.get("percentile_99") is not None and sofr.get("percentile_1") is not None:
        repo_dispersion_bps = (float(sofr["percentile_99"]) - float(sofr["percentile_1"])) * 100.0
        metrics["repo_dispersion_bps"] = round(repo_dispersion_bps, 2)

    if sofr and sofr.get("volume_billions") is not None:
        metrics["sofr_volume_billions"] = float(sofr["volume_billions"])
    if effr.get("volume_billions") is not None:
        metrics["effr_volume_billions"] = float(effr["volume_billions"])

    bias = policy_rate_bias(
        sofr_effr_spread_bps=sofr_effr_spread_bps,
        repo_dispersion_bps=repo_dispersion_bps,
        band_position=band_position,
    )
    metrics["policy_rate_bias"] = bias
    metrics["policy_rate_heat_score"] = float(_score_from_bias(bias))
    return metrics


def _load_rates_by_date(db_path: str | Path) -> dict[str, dict[str, dict[str, float | None]]]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT effective_date, rate_type, percent_rate, percentile_1, percentile_99,
                   target_rate_from, target_rate_to, volume_billions
            FROM policy_rate_observations
            ORDER BY effective_date ASC
            """
        ).fetchall()
    except sqlite3.DatabaseError:
        return {}
    finally:
        connection.close()

    by_date: dict[str, dict[str, dict[str, float | None]]] = {}
    for row in rows:
        by_date.setdefault(str(row["effective_date"]), {})[str(row["rate_type"])] = {
            "percent_rate": _coerce_float(row["percent_rate"]),
            "percentile_1": _coerce_float(row["percentile_1"]),
            "percentile_99": _coerce_float(row["percentile_99"]),
            "target_rate_from": _coerce_float(row["target_rate_from"]),
            "target_rate_to": _coerce_float(row["target_rate_to"]),
            "volume_billions": _coerce_float(row["volume_billions"]),
        }
    return by_date


def build_policy_rate_bias_history(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    limit: int = 400,
) -> list[dict[str, object]]:
    """Daily funding-stress bias, oldest first, in the same units as the other domains."""
    by_date = _load_rates_by_date(db_path)
    history: list[dict[str, object]] = []
    for effective_date in sorted(by_date):
        metrics = _metrics_for_date(by_date[effective_date])
        if metrics is None:
            continue
        history.append(
            {
                "date": effective_date,
                "bias": metrics["policy_rate_bias"],
                "effr": metrics.get("effr"),
                "sofr": metrics.get("sofr"),
                "sofr_effr_spread_bps": metrics.get("sofr_effr_spread_bps"),
                "repo_dispersion_bps": metrics.get("repo_dispersion_bps"),
            }
        )
    return history[-max(1, limit) :]


def build_dashboard_policy_rates_payload(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    row_limit: int = 64,
) -> dict[str, object] | None:
    by_date = _load_rates_by_date(db_path)
    if not by_date:
        return None
    latest_date = max(by_date)
    metrics = _metrics_for_date(by_date[latest_date])
    if metrics is None:
        return None

    latest_values = []
    for rate_type, values in sorted(by_date[latest_date].items()):
        family, label, description = RATE_TYPES.get(rate_type, ("", rate_type, ""))
        latest_values.append(
            {
                "series_id": rate_type,
                "label": label,
                "category": f"{family} overnight",
                "description": description,
                "value": values.get("percent_rate"),
                "volume_billions": values.get("volume_billions"),
            }
        )

    heat_card = _build_policy_rate_heat_card(metric_date=latest_date, metrics=metrics)
    return {
        "generated_at": _utcnow().isoformat(),
        "metric_date": latest_date,
        "provider": PROVIDER,
        "metrics": metrics,
        "metric_metadata": {
            "policy_rate_bias": {
                "formula": (
                    "-(0.45*sofr_effr_spread + 0.30*repo_dispersion + 0.25*effr_band_position)"
                ),
                "note": "Funding stress reads dovish, so the stress score is negated.",
                "neutral_assumptions": {
                    "sofr_effr_spread_bps": NEUTRAL_SOFR_EFFR_SPREAD_BPS,
                    "repo_dispersion_bps": NEUTRAL_REPO_DISPERSION_BPS,
                    "effr_band_position": NEUTRAL_BAND_POSITION,
                },
            }
        },
        "heat_card": heat_card,
        "latest_values": latest_values[: max(1, row_limit)],
        "bias_history": build_policy_rate_bias_history(db_path=db_path),
        "source_url": NYFED_HOME_URL,
    }


def _build_policy_rate_heat_card(*, metric_date: str, metrics: Mapping[str, float]) -> dict[str, object]:
    bias = float(metrics.get("policy_rate_bias", 0.0))
    score = _score_from_bias(bias)
    tone = _tone_from_score(score)
    sign = "+" if bias > 0 else ""
    pills = [f"NY Fed {metric_date}", f"bias {sign}{bias:.2f}"]
    if "effr" in metrics:
        pills.append(f"EFFR {metrics['effr']:.2f}%")
    if "target_rate_from" in metrics and "target_rate_to" in metrics:
        pills.append(f"band {metrics['target_rate_from']:.2f}-{metrics['target_rate_to']:.2f}%")
    if "sofr_effr_spread_bps" in metrics:
        pills.append(f"SOFR-EFFR {metrics['sofr_effr_spread_bps']:+.0f}bp")
    return {
        "label": "Funding Stress",
        "display": f"{sign}{bias:.2f}",
        "score": score,
        "tone": tone,
        "toneLabel": "money markets",
        "sources": pills,
    }


def build_policy_rate_knowledge_graph_payload(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    per_type_limit: int = 8,
) -> dict[str, object] | None:
    by_date = _load_rates_by_date(db_path)
    if not by_date:
        return None

    dates = sorted(by_date)
    recent_dates = dates[-max(1, per_type_limit) :]
    nodes: list[dict[str, object]] = []
    edges: list[dict[str, object]] = []

    nodes.append(
        {
            "id": "policy-rate-hub",
            "kind": "policy_rate_hub",
            "label": "Money Markets",
            "sublabel": PROVIDER,
            "x": -5.1,
            "y": 2.9,
            "z": 0.0,
            "size": 0.24,
            "color": "#d2a8ff",
        }
    )
    edges.append({"source": "policy-rate-hub", "target": "fomc-hub", "color": "#d2a8ff", "opacity": 0.3})

    observed_types = sorted({rate for value in by_date.values() for rate in value})
    obs_count = 0
    for index, rate_type in enumerate(observed_types):
        series_id = f"policy-rate-series-{rate_type.lower()}"
        angle = (index / max(1, len(observed_types))) * math.tau
        series_x = -5.1 + 1.9 * math.cos(angle)
        series_y = 2.9 + 1.2 * math.sin(angle)
        label = RATE_TYPES.get(rate_type, ("", rate_type, ""))[1]
        nodes.append(
            {
                "id": series_id,
                "kind": "policy_rate_series",
                "label": rate_type,
                "sublabel": label,
                "x": round(series_x, 3),
                "y": round(series_y, 3),
                "z": 0.0,
                "size": 0.14,
                "color": "#d2a8ff",
            }
        )
        edges.append({"source": "policy-rate-hub", "target": series_id, "color": "#d2a8ff", "opacity": 0.28})

        previous_obs_id = None
        for depth, effective_date in enumerate(recent_dates):
            values = by_date[effective_date].get(rate_type)
            if not values or values.get("percent_rate") is None:
                continue
            obs_id = f"policy-rate-{rate_type.lower()}-{effective_date}"
            obs_count += 1
            nodes.append(
                {
                    "id": obs_id,
                    "kind": "policy_rate_observation",
                    "label": effective_date,
                    "sublabel": f"{values['percent_rate']:.3f}%",
                    "x": round(series_x + 0.42 * math.cos(angle), 3),
                    "y": round(series_y - 0.32 * (depth + 1), 3),
                    "z": round(0.34 * (depth + 1), 3),
                    "size": 0.055 if depth < len(recent_dates) - 1 else 0.085,
                    "color": "#d2a8ff",
                }
            )
            edges.append({"source": series_id, "target": obs_id, "color": "#d2a8ff", "opacity": 0.1})
            if previous_obs_id is not None:
                edges.append({"source": previous_obs_id, "target": obs_id, "color": "#d2a8ff", "opacity": 0.22})
            previous_obs_id = obs_id

    return {
        "generated_at": _utcnow().isoformat(),
        "policy_rate_graph": {
            "nodes": nodes,
            "edges": edges,
            "stats": {
                "series_count": len(observed_types),
                "observation_count": obs_count,
                "edge_count": len(edges),
            },
        },
    }


def export_dashboard_policy_rates_js(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    output_js_path: str | Path,
    per_type_points: int = 8,
    raw_row_limit: int = 64,
) -> dict[str, object] | None:
    payload = build_dashboard_policy_rates_payload(db_path=db_path, row_limit=raw_row_limit)
    if payload is None:
        return None
    graph_payload = build_policy_rate_knowledge_graph_payload(db_path=db_path, per_type_limit=per_type_points)
    if graph_payload:
        payload = {**payload, **graph_payload}
    output_path = Path(output_js_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        "window.__POLICY_RATE_DASHBOARD_DATA__ = "
        + json.dumps(payload, indent=2, sort_keys=True)
        + ";\n",
        encoding="utf-8",
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest NY Fed overnight reference rates (EFFR, SOFR, OBFR, BGCR, TGCR) into SQLite."
    )
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="SQLite database path.")
    parser.add_argument("--start-date", help="ISO start date (default: Jan 1 two years ago).")
    parser.add_argument("--end-date", help="ISO end date (default: today).")
    parser.add_argument(
        "--rate-type",
        action="append",
        dest="rate_types",
        choices=sorted(RATE_TYPES.keys()),
        help="Restrict to specific rate types. Repeat to select several.",
    )
    parser.add_argument("--dashboard-js", help="Optional output path for the dashboard payload.")
    parser.add_argument(
        "--kg-points-per-type",
        type=int,
        default=8,
        help="Number of most-recent dates per rate type in the graph payload.",
    )
    parser.add_argument(
        "--raw-row-limit",
        type=int,
        default=64,
        help="Max rows to include in the Raw Data table payload.",
    )
    args = parser.parse_args(argv)

    service = PolicyRateIngestionService(store=PolicyRateStore(args.db_path))
    result = service.ingest(
        start_date=args.start_date,
        end_date=args.end_date,
        rate_types=args.rate_types,
    )
    if args.dashboard_js:
        export_dashboard_policy_rates_js(
            db_path=args.db_path,
            output_js_path=args.dashboard_js,
            per_type_points=args.kg_points_per_type,
            raw_row_limit=args.raw_row_limit,
        )

    print(
        f"Ingested {result['observation_count']} NY Fed reference rate points "
        f"({result['start_date']} to {result['end_date']}, latest={result['effective_date']})."
    )
    return 0


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


def _coerce_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _get_json(url: str) -> object:
    request = Request(
        url,
        headers={
            "User-Agent": "WhatTheFed/1.0 (+https://github.com/calegeffre/WhatTheFed)",
            "Accept": "application/json",
        },
        method="GET",
    )
    with urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


if __name__ == "__main__":
    raise SystemExit(main())
