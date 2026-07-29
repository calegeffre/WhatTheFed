from __future__ import annotations

import argparse
import contextlib
import json
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.request import Request, urlopen

from .rag import Document


JsonFetcher = Callable[[str], Mapping[str, object] | list[object]]
DEFAULT_DB_PATH = Path("data") / "market_snapshots.db"
KALSHI_API_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
POLYMARKET_API_BASE_URL = "https://gamma-api.polymarket.com"


class MarketIngestionError(RuntimeError):
    pass


@dataclass(frozen=True)
class MarketOutcome:
    key: str
    label: str
    probability: float | None = None
    canonical_label: str | None = None


@dataclass(frozen=True)
class MarketSnapshot:
    provider: str
    market_id: str
    market_name: str
    target_meeting: str | None
    published_at: str
    source_url: str
    status: str | None = None
    close_time: str | None = None
    last_price: float | None = None
    volume: float | None = None
    liquidity: float | None = None
    outcomes: tuple[MarketOutcome, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def canonical_probabilities(self) -> dict[str, float]:
        probabilities: dict[str, float] = {}
        for outcome in self.outcomes:
            if outcome.canonical_label is None or outcome.probability is None:
                continue
            probabilities[outcome.canonical_label] = round(
                probabilities.get(outcome.canonical_label, 0.0) + outcome.probability,
                4,
            )
        return probabilities


@dataclass(frozen=True)
class MarketWatchConfig:
    provider: str
    market_ref: str
    target_meeting: str | None = None
    source_url: str | None = None
    market_name: str | None = None
    outcome_mappings: Mapping[str, str] = field(default_factory=dict)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def canonical_label_for(self, outcome_key: str, outcome_label: str) -> str | None:
        mapping = {key.lower(): value for key, value in self.outcome_mappings.items()}
        return mapping.get(outcome_key.lower()) or mapping.get(outcome_label.lower())

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> MarketWatchConfig:
        return cls(
            provider=str(payload["provider"]),
            market_ref=str(payload["market_ref"]),
            target_meeting=_optional_string(payload.get("target_meeting")),
            source_url=_optional_string(payload.get("source_url")),
            market_name=_optional_string(payload.get("market_name")),
            outcome_mappings=_coerce_mapping(payload.get("outcome_mappings")),
            metadata=_coerce_metadata(payload.get("metadata")),
        )


class KalshiMarketClient:
    def __init__(
        self,
        fetch_json: JsonFetcher | None = None,
        base_url: str = KALSHI_API_BASE_URL,
    ) -> None:
        self.fetch_json = fetch_json or _fetch_json
        self.base_url = base_url.rstrip("/")

    def fetch_snapshot(self, config: MarketWatchConfig) -> MarketSnapshot:
        payload = self.fetch_json(f"{self.base_url}/markets/{config.market_ref}")
        if not isinstance(payload, Mapping):
            raise MarketIngestionError("Kalshi market endpoint returned a non-object payload.")
        market = payload.get("market")
        if not isinstance(market, Mapping):
            raise MarketIngestionError("Kalshi market endpoint did not include a market object.")

        return _build_kalshi_snapshot(
            market=market,
            config=config,
            source_url=config.source_url or f"{self.base_url}/markets/{config.market_ref}",
            target_meeting=config.target_meeting,
            extra_metadata=dict(config.metadata),
        )


class KalshiEventClient:
    def __init__(
        self,
        fetch_json: JsonFetcher | None = None,
        base_url: str = KALSHI_API_BASE_URL,
    ) -> None:
        self.fetch_json = fetch_json or _fetch_json
        self.base_url = base_url.rstrip("/")

    def fetch_snapshots(self, config: MarketWatchConfig) -> list[MarketSnapshot]:
        payload = self.fetch_json(f"{self.base_url}/events/{config.market_ref}")
        if not isinstance(payload, Mapping):
            raise MarketIngestionError("Kalshi event endpoint returned a non-object payload.")
        event = payload.get("event")
        markets = payload.get("markets")
        if not isinstance(event, Mapping) or not isinstance(markets, list):
            raise MarketIngestionError("Kalshi event endpoint did not include event and markets payloads.")

        event_ticker = str(event.get("event_ticker") or config.market_ref)
        target_meeting = config.target_meeting or _date_only(_optional_string(event.get("strike_date")))
        snapshots: list[MarketSnapshot] = []
        for raw_market in markets:
            if not isinstance(raw_market, Mapping):
                continue
            snapshots.append(
                _build_kalshi_snapshot(
                    market=raw_market,
                    config=config,
                    source_url=config.source_url or f"{self.base_url}/events/{event_ticker}",
                    target_meeting=target_meeting,
                    extra_metadata={
                        **dict(config.metadata),
                        "event_ticker": event_ticker,
                        "event_title": event.get("title"),
                    },
                    inferred_yes_label=_infer_kalshi_yes_canonical_label(raw_market),
                )
            )
        return snapshots


class PolymarketMarketClient:
    def __init__(
        self,
        fetch_json: JsonFetcher | None = None,
        base_url: str = POLYMARKET_API_BASE_URL,
    ) -> None:
        self.fetch_json = fetch_json or _fetch_json
        self.base_url = base_url.rstrip("/")

    def fetch_snapshot(self, config: MarketWatchConfig) -> MarketSnapshot:
        payload = self.fetch_json(f"{self.base_url}/markets/slug/{config.market_ref}")
        if not isinstance(payload, Mapping):
            raise MarketIngestionError("Polymarket market endpoint returned a non-object payload.")

        labels = _coerce_sequence(payload.get("outcomes"))
        prices = _coerce_sequence(payload.get("outcomePrices"))
        outcomes = tuple(
            MarketOutcome(
                key=str(index),
                label=str(label),
                probability=_coerce_float(prices[index]) if index < len(prices) else None,
                canonical_label=config.canonical_label_for(str(index), str(label)),
            )
            for index, label in enumerate(labels)
        )
        market_id = str(payload.get("conditionId") or payload.get("id") or config.market_ref)
        return MarketSnapshot(
            provider="polymarket",
            market_id=market_id,
            market_name=str(payload.get("question") or config.market_name or config.market_ref),
            target_meeting=config.target_meeting,
            published_at=str(payload.get("updatedAt") or payload.get("createdAt") or _utcnow().isoformat()),
            source_url=config.source_url or f"{self.base_url}/markets/slug/{config.market_ref}",
            status="active" if bool(payload.get("active")) else "inactive",
            close_time=_optional_string(payload.get("endDate")),
            last_price=_coerce_float(payload.get("lastTradePrice")),
            volume=_coerce_float(payload.get("volume")),
            liquidity=_coerce_float(payload.get("liquidity")),
            outcomes=outcomes,
            metadata={
                **dict(config.metadata),
                "slug": payload.get("slug"),
                "condition_id": payload.get("conditionId"),
                "raw_market": dict(payload),
            },
        )


class PolymarketEventClient:
    def __init__(
        self,
        fetch_json: JsonFetcher | None = None,
        base_url: str = POLYMARKET_API_BASE_URL,
    ) -> None:
        self.fetch_json = fetch_json or _fetch_json
        self.base_url = base_url.rstrip("/")

    def fetch_snapshots(self, config: MarketWatchConfig) -> list[MarketSnapshot]:
        payload = self.fetch_json(f"{self.base_url}/events/slug/{config.market_ref}")
        if not isinstance(payload, Mapping):
            raise MarketIngestionError("Polymarket event endpoint returned a non-object payload.")
        markets = payload.get("markets")
        if not isinstance(markets, list):
            raise MarketIngestionError("Polymarket event endpoint did not include a markets array.")

        event_slug = str(payload.get("slug") or config.market_ref)
        event_title = str(payload.get("title") or config.market_name or event_slug)
        target_meeting = config.target_meeting or _date_only(_optional_string(payload.get("endDate")))
        snapshots: list[MarketSnapshot] = []
        for market in markets:
            if not isinstance(market, Mapping):
                continue
            labels = _coerce_sequence(market.get("outcomes"))
            prices = _coerce_sequence(market.get("outcomePrices"))
            inferred_yes_label = _infer_polymarket_yes_canonical_label(market)
            outcomes = tuple(
                MarketOutcome(
                    key=str(index),
                    label=str(label),
                    probability=_coerce_float(prices[index]) if index < len(prices) else None,
                    canonical_label=(
                        config.canonical_label_for(str(index), str(label))
                        or (inferred_yes_label if index == 0 else None)
                    ),
                )
                for index, label in enumerate(labels)
            )
            market_id = str(market.get("conditionId") or market.get("id") or market.get("slug"))
            snapshots.append(
                MarketSnapshot(
                    provider="polymarket",
                    market_id=market_id,
                    market_name=str(market.get("question") or event_title),
                    target_meeting=target_meeting,
                    published_at=str(market.get("updatedAt") or payload.get("updatedAt") or _utcnow().isoformat()),
                    source_url=config.source_url or f"https://polymarket.com/event/{event_slug}",
                    status="active" if bool(market.get("active")) else "inactive",
                    close_time=_optional_string(market.get("endDate") or payload.get("endDate")),
                    last_price=_coerce_float(market.get("lastTradePrice")),
                    volume=_coerce_float(market.get("volume")),
                    liquidity=_coerce_float(market.get("liquidity")),
                    outcomes=outcomes,
                    metadata={
                        **dict(config.metadata),
                        "event_slug": event_slug,
                        "event_title": event_title,
                        "market_slug": market.get("slug"),
                        "group_item_title": market.get("groupItemTitle"),
                        "raw_market": dict(market),
                    },
                )
            )
        return snapshots


class MarketSnapshotStore:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._open_connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS market_snapshots (
                    provider TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    market_name TEXT NOT NULL,
                    target_meeting TEXT,
                    published_at TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    status TEXT,
                    close_time TEXT,
                    last_price REAL,
                    volume REAL,
                    liquidity REAL,
                    canonical_probabilities_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    ingested_at TEXT NOT NULL,
                    PRIMARY KEY (provider, market_id, published_at)
                );

                CREATE TABLE IF NOT EXISTS market_outcomes (
                    provider TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    outcome_key TEXT NOT NULL,
                    outcome_label TEXT NOT NULL,
                    probability REAL,
                    canonical_label TEXT,
                    PRIMARY KEY (provider, market_id, published_at, outcome_key)
                );

                CREATE TABLE IF NOT EXISTS ingestion_runs (
                    run_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    market_ref TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    error_message TEXT,
                    snapshot_key TEXT
                );
                """
            )

    def write_snapshot(self, snapshot: MarketSnapshot) -> None:
        self.initialize()
        ingested_at = _utcnow().isoformat()
        with self._open_connection() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO market_snapshots (
                    provider, market_id, market_name, target_meeting, published_at, source_url,
                    status, close_time, last_price, volume, liquidity,
                    canonical_probabilities_json, metadata_json, ingested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.provider,
                    snapshot.market_id,
                    snapshot.market_name,
                    snapshot.target_meeting,
                    snapshot.published_at,
                    snapshot.source_url,
                    snapshot.status,
                    snapshot.close_time,
                    snapshot.last_price,
                    snapshot.volume,
                    snapshot.liquidity,
                    json.dumps(snapshot.canonical_probabilities(), sort_keys=True),
                    json.dumps(dict(snapshot.metadata), sort_keys=True),
                    ingested_at,
                ),
            )
            connection.execute(
                """
                DELETE FROM market_outcomes
                WHERE provider = ? AND market_id = ? AND published_at = ?
                """,
                (snapshot.provider, snapshot.market_id, snapshot.published_at),
            )
            connection.executemany(
                """
                INSERT INTO market_outcomes (
                    provider, market_id, published_at, outcome_key, outcome_label, probability, canonical_label
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        snapshot.provider,
                        snapshot.market_id,
                        snapshot.published_at,
                        outcome.key,
                        outcome.label,
                        outcome.probability,
                        outcome.canonical_label,
                    )
                    for outcome in snapshot.outcomes
                ],
            )

    def record_run(
        self,
        *,
        run_id: str,
        provider: str,
        market_ref: str,
        started_at: str,
        status: str,
        completed_at: str | None = None,
        error_message: str | None = None,
        snapshot_key: str | None = None,
    ) -> None:
        self.initialize()
        with self._open_connection() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO ingestion_runs (
                    run_id, provider, market_ref, started_at, completed_at, status, error_message, snapshot_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, provider, market_ref, started_at, completed_at, status, error_message, snapshot_key),
            )

    def load_documents(
        self,
        *,
        provider: str | None = None,
        target_meeting: str | None = None,
        limit: int = 100,
    ) -> list[Document]:
        self.initialize()
        query = """
            SELECT provider, market_id, market_name, target_meeting, published_at, source_url,
                   status, close_time, last_price, volume, liquidity,
                   canonical_probabilities_json, metadata_json
            FROM market_snapshots
        """
        clauses: list[str] = []
        parameters: list[object] = []
        if provider is not None:
            clauses.append("provider = ?")
            parameters.append(provider)
        if target_meeting is not None:
            clauses.append("target_meeting = ?")
            parameters.append(target_meeting)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY published_at DESC LIMIT ?"
        parameters.append(limit)

        with self._open_connection() as connection:
            rows = connection.execute(query, parameters).fetchall()

            documents: list[Document] = []
            for row in rows:
                outcome_rows = connection.execute(
                    """
                    SELECT outcome_key, outcome_label, probability, canonical_label
                    FROM market_outcomes
                    WHERE provider = ? AND market_id = ? AND published_at = ?
                    ORDER BY outcome_key
                    """,
                    (row["provider"], row["market_id"], row["published_at"]),
                ).fetchall()
                snapshot = MarketSnapshot(
                    provider=row["provider"],
                    market_id=row["market_id"],
                    market_name=row["market_name"],
                    target_meeting=row["target_meeting"],
                    published_at=row["published_at"],
                    source_url=row["source_url"],
                    status=row["status"],
                    close_time=row["close_time"],
                    last_price=row["last_price"],
                    volume=row["volume"],
                    liquidity=row["liquidity"],
                    outcomes=tuple(
                        MarketOutcome(
                            key=outcome_row["outcome_key"],
                            label=outcome_row["outcome_label"],
                            probability=outcome_row["probability"],
                            canonical_label=outcome_row["canonical_label"],
                        )
                        for outcome_row in outcome_rows
                    ),
                    metadata=json.loads(row["metadata_json"]),
                )
                documents.append(snapshot_to_document(snapshot))
            return documents

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


class MarketIngestionService:
    def __init__(
        self,
        store: MarketSnapshotStore,
        kalshi_client: KalshiMarketClient | None = None,
        polymarket_client: PolymarketMarketClient | None = None,
    ) -> None:
        self.store = store
        self.clients = {
            "kalshi": kalshi_client or KalshiMarketClient(),
            "kalshi_event": KalshiEventClient(),
            "polymarket": polymarket_client or PolymarketMarketClient(),
            "polymarket_event": PolymarketEventClient(),
        }

    def ingest(self, watchlist: Iterable[MarketWatchConfig]) -> list[MarketSnapshot]:
        snapshots: list[MarketSnapshot] = []
        for config in watchlist:
            client = self.clients.get(config.provider.lower())
            if client is None:
                raise MarketIngestionError(f"Unsupported provider: {config.provider}")

            run_id = str(uuid.uuid4())
            started_at = _utcnow().isoformat()
            self.store.record_run(
                run_id=run_id,
                provider=config.provider.lower(),
                market_ref=config.market_ref,
                started_at=started_at,
                status="started",
            )

            try:
                fetched_snapshots = self._fetch_snapshots(client, config)
                for snapshot in fetched_snapshots:
                    self.store.write_snapshot(snapshot)
            except Exception as exc:
                self.store.record_run(
                    run_id=run_id,
                    provider=config.provider.lower(),
                    market_ref=config.market_ref,
                    started_at=started_at,
                    completed_at=_utcnow().isoformat(),
                    status="failed",
                    error_message=str(exc),
                )
                raise

            snapshot_keys = [
                f"{snapshot.provider}:{snapshot.market_id}:{snapshot.published_at}"
                for snapshot in fetched_snapshots
            ]
            self.store.record_run(
                run_id=run_id,
                provider=config.provider.lower(),
                market_ref=config.market_ref,
                started_at=started_at,
                completed_at=_utcnow().isoformat(),
                status="completed",
                snapshot_key=";".join(snapshot_keys),
            )
            snapshots.extend(fetched_snapshots)
        return snapshots

    @staticmethod
    def _fetch_snapshots(client: object, config: MarketWatchConfig) -> list[MarketSnapshot]:
        if hasattr(client, "fetch_snapshots"):
            values = client.fetch_snapshots(config)  # type: ignore[attr-defined]
            return list(values)
        if hasattr(client, "fetch_snapshot"):
            value = client.fetch_snapshot(config)  # type: ignore[attr-defined]
            return [value]
        raise MarketIngestionError("Configured client does not provide snapshot fetch methods.")


def load_watchlist(path: str | Path) -> list[MarketWatchConfig]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise MarketIngestionError("Watchlist file must contain a JSON array.")
    return [MarketWatchConfig.from_dict(item) for item in payload if isinstance(item, Mapping)]


def snapshot_to_document(snapshot: MarketSnapshot) -> Document:
    canonical_probabilities = snapshot.canonical_probabilities()
    outcomes_text = ", ".join(
        f"{outcome.label}={_format_probability(outcome.probability)}"
        for outcome in snapshot.outcomes
        if outcome.probability is not None
    )
    canonical_text = ", ".join(
        f"{label}={_format_probability(probability)}"
        for label, probability in sorted(canonical_probabilities.items())
    )
    content = (
        f"{snapshot.provider.title()} market snapshot for {snapshot.market_name}. "
        f"Target meeting: {snapshot.target_meeting or 'unassigned'}. "
        f"Observed at {snapshot.published_at}. "
        f"Outcomes: {outcomes_text or 'unavailable'}. "
        f"Canonical probabilities: {canonical_text or 'unmapped'}."
    )
    return Document(
        source=f"{snapshot.provider}_{snapshot.market_id}_{_slugify(snapshot.published_at)}",
        content=content,
        kind=f"{snapshot.provider}_market",
        published_at=snapshot.published_at,
        source_url=snapshot.source_url,
        metadata={
            "provider": snapshot.provider.title(),
            "market_id": snapshot.market_id,
            "market_name": snapshot.market_name,
            "target_meeting": snapshot.target_meeting,
            "status": snapshot.status,
            "close_time": snapshot.close_time,
            "last_price": snapshot.last_price,
            "volume": snapshot.volume,
            "liquidity": snapshot.liquidity,
            "canonical_probabilities": canonical_probabilities,
            "outcomes": [asdict(outcome) for outcome in snapshot.outcomes],
        },
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest Kalshi and Polymarket market snapshots into SQLite.")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="SQLite database path.")
    parser.add_argument("--watchlist", required=True, help="Path to a JSON watchlist file.")
    args = parser.parse_args(argv)

    store = MarketSnapshotStore(args.db_path)
    service = MarketIngestionService(store=store)
    snapshots = service.ingest(load_watchlist(args.watchlist))
    print(f"Ingested {len(snapshots)} market snapshots into {args.db_path}.")
    return 0


def _fetch_json(url: str) -> Mapping[str, object] | list[object]:
    request = Request(url, headers={"User-Agent": "WhatTheFed/1.0"})
    with urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, (dict, list)):
        raise MarketIngestionError(f"Expected JSON object or array from {url}.")
    return payload


def _coerce_mapping(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): str(val) for key, val in value.items()}


def _coerce_metadata(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): val for key, val in value.items()}


def _coerce_sequence(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return [value]
        return payload if isinstance(payload, list) else [payload]
    return []


def _coerce_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_number(*values: object) -> float | None:
    for value in values:
        parsed = _coerce_float(value)
        if parsed is not None:
            return parsed
    return None


def _optional_string(value: object) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _format_probability(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{round(value * 100, 2)}%"


def _slugify(value: str) -> str:
    return (
        value.replace(":", "")
        .replace("-", "")
        .replace(".", "")
        .replace("T", "_")
        .replace("Z", "z")
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _build_kalshi_snapshot(
    *,
    market: Mapping[str, object],
    config: MarketWatchConfig,
    source_url: str,
    target_meeting: str | None,
    extra_metadata: Mapping[str, object],
    inferred_yes_label: str | None = None,
) -> MarketSnapshot:
    yes_probability = _first_number(
        market.get("last_price_dollars"),
        market.get("yes_ask_dollars"),
        market.get("yes_bid_dollars"),
    )
    outcomes = _build_binary_outcomes(
        config=config,
        yes_probability=yes_probability,
        inferred_yes_label=inferred_yes_label,
    )
    market_id = str(market.get("ticker") or config.market_ref)
    return MarketSnapshot(
        provider="kalshi",
        market_id=market_id,
        market_name=str(market.get("title") or config.market_name or market_id),
        target_meeting=target_meeting,
        published_at=str(market.get("updated_time") or _utcnow().isoformat()),
        source_url=source_url,
        status=_optional_string(market.get("status")),
        close_time=_optional_string(market.get("close_time") or market.get("expiration_time")),
        last_price=yes_probability,
        volume=_first_number(market.get("volume_fp"), market.get("volume")),
        liquidity=_first_number(market.get("liquidity_dollars"), market.get("liquidity")),
        outcomes=outcomes,
        metadata={
            **dict(extra_metadata),
            "event_ticker": market.get("event_ticker"),
            "market_type": market.get("market_type"),
            "subtitle": market.get("subtitle"),
            "yes_sub_title": market.get("yes_sub_title"),
            "raw_market": dict(market),
        },
    )


def _build_binary_outcomes(
    *,
    config: MarketWatchConfig,
    yes_probability: float | None,
    inferred_yes_label: str | None = None,
) -> tuple[MarketOutcome, ...]:
    no_probability = round(1.0 - yes_probability, 4) if yes_probability is not None else None
    return (
        MarketOutcome(
            key="yes",
            label="Yes",
            probability=yes_probability,
            canonical_label=config.canonical_label_for("yes", "Yes") or inferred_yes_label,
        ),
        MarketOutcome(
            key="no",
            label="No",
            probability=no_probability,
            canonical_label=config.canonical_label_for("no", "No"),
        ),
    )


def _infer_kalshi_yes_canonical_label(market: Mapping[str, object]) -> str | None:
    text = " ".join(
        value
        for value in (
            _optional_string(market.get("yes_sub_title")) or "",
            _optional_string(market.get("subtitle")) or "",
            _optional_string(market.get("title")) or "",
        )
        if value
    ).lower()
    return _infer_canonical_label_from_text(text)


def _infer_polymarket_yes_canonical_label(market: Mapping[str, object]) -> str | None:
    text = " ".join(
        value
        for value in (
            _optional_string(market.get("groupItemTitle")) or "",
            _optional_string(market.get("question")) or "",
        )
        if value
    ).lower()
    return _infer_canonical_label_from_text(text)


def _infer_canonical_label_from_text(text: str) -> str | None:
    if ("maintain" in text) or ("no change" in text) or ("hike 0" in text):
        return "hold"
    if ("cut" in text) or ("decrease" in text):
        return "cut"
    if ("hike" in text) or ("increase" in text):
        return "raise"
    return None


def _date_only(value: str | None) -> str | None:
    if value is None:
        return None
    return value[:10]


if __name__ == "__main__":
    raise SystemExit(main())
