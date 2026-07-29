import tempfile
import unittest
from pathlib import Path

from whatthefed import (
    KalshiEventClient,
    KalshiMarketClient,
    MarketIngestionService,
    MarketSnapshotStore,
    MarketWatchConfig,
    PolymarketEventClient,
    PolymarketMarketClient,
    load_watchlist,
)


class MarketIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.kalshi_payload = {
            "market": {
                "ticker": "FEDSEP2026-HOLD",
                "title": "Will the Fed hold rates in September 2026?",
                "status": "active",
                "updated_time": "2026-07-28T12:00:00Z",
                "close_time": "2026-09-16T18:00:00Z",
                "last_price_dollars": "0.6400",
                "yes_bid_dollars": "0.6300",
                "yes_ask_dollars": "0.6500",
                "volume_fp": "24500.00",
                "liquidity_dollars": "8450.0000",
                "event_ticker": "FEDSEP2026",
                "market_type": "binary",
            }
        }
        self.polymarket_payload = {
            "conditionId": "0xabc123",
            "question": "Will the Fed cut rates by September 2026?",
            "slug": "fed-cut-by-september-2026",
            "active": True,
            "updatedAt": "2026-07-28T12:05:00Z",
            "endDate": "2026-09-16T18:00:00Z",
            "volume": "99321.4",
            "liquidity": "14025.55",
            "lastTradePrice": 0.27,
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.27", "0.73"]',
        }
        self.kalshi_event_payload = {
            "event": {
                "event_ticker": "KXFEDDECISION-26SEP",
                "title": "Fed decision in Sep 2026?",
                "strike_date": "2026-09-16T18:00:00Z",
            },
            "markets": [
                {
                    "ticker": "KXFEDDECISION-26SEP-H0",
                    "title": "Will the Federal Reserve Hike rates by 0bps at their September 2026 meeting?",
                    "subtitle": "Hike 0bps",
                    "yes_sub_title": "Fed maintains rate",
                    "status": "active",
                    "updated_time": "2026-07-28T12:00:00Z",
                    "close_time": "2026-09-16T17:59:00Z",
                    "last_price_dollars": "0.4200",
                    "volume_fp": "12000.00",
                    "liquidity_dollars": "9100.0000",
                    "event_ticker": "KXFEDDECISION-26SEP",
                    "market_type": "binary",
                },
                {
                    "ticker": "KXFEDDECISION-26SEP-H25",
                    "title": "Will the Federal Reserve Hike rates by 25bps at their September 2026 meeting?",
                    "subtitle": "Hike 25bps",
                    "yes_sub_title": "Hike 25bps",
                    "status": "active",
                    "updated_time": "2026-07-28T12:00:00Z",
                    "close_time": "2026-09-16T17:59:00Z",
                    "last_price_dollars": "0.5400",
                    "volume_fp": "15000.00",
                    "liquidity_dollars": "10400.0000",
                    "event_ticker": "KXFEDDECISION-26SEP",
                    "market_type": "binary",
                },
            ],
        }
        self.polymarket_event_payload = {
            "slug": "fed-decision-in-september-762",
            "title": "Fed Decision in September?",
            "endDate": "2026-09-16T00:00:00Z",
            "updatedAt": "2026-07-28T12:05:00Z",
            "markets": [
                {
                    "conditionId": "0xa1",
                    "question": "Will there be no change in Fed interest rates after the September 2026 meeting?",
                    "groupItemTitle": "No change",
                    "slug": "no-change-sep-2026",
                    "active": True,
                    "updatedAt": "2026-07-28T12:05:00Z",
                    "endDate": "2026-09-16T00:00:00Z",
                    "volume": "5000",
                    "liquidity": "1100",
                    "lastTradePrice": 0.4,
                    "outcomes": "[\"Yes\", \"No\"]",
                    "outcomePrices": "[\"0.40\", \"0.60\"]",
                },
                {
                    "conditionId": "0xa2",
                    "question": "Will the Fed increase interest rates by 25 bps after the September 2026 meeting?",
                    "groupItemTitle": "25 bps increase",
                    "slug": "hike-25-sep-2026",
                    "active": True,
                    "updatedAt": "2026-07-28T12:05:00Z",
                    "endDate": "2026-09-16T00:00:00Z",
                    "volume": "7500",
                    "liquidity": "1800",
                    "lastTradePrice": 0.56,
                    "outcomes": "[\"Yes\", \"No\"]",
                    "outcomePrices": "[\"0.56\", \"0.44\"]",
                },
            ],
        }

    def test_kalshi_client_parses_snapshot(self) -> None:
        client = KalshiMarketClient(fetch_json=lambda _: self.kalshi_payload)
        snapshot = client.fetch_snapshot(
            MarketWatchConfig(
                provider="kalshi",
                market_ref="FEDSEP2026-HOLD",
                target_meeting="2026-09-16",
                outcome_mappings={"yes": "hold"},
            )
        )
        self.assertEqual(snapshot.provider, "kalshi")
        self.assertEqual(snapshot.market_id, "FEDSEP2026-HOLD")
        self.assertEqual(snapshot.last_price, 0.64)
        self.assertEqual(snapshot.canonical_probabilities()["hold"], 0.64)
        self.assertEqual(snapshot.outcomes[1].probability, 0.36)

    def test_polymarket_client_parses_snapshot(self) -> None:
        client = PolymarketMarketClient(fetch_json=lambda _: self.polymarket_payload)
        snapshot = client.fetch_snapshot(
            MarketWatchConfig(
                provider="polymarket",
                market_ref="fed-cut-by-september-2026",
                target_meeting="2026-09-16",
                outcome_mappings={"yes": "cut"},
            )
        )
        self.assertEqual(snapshot.provider, "polymarket")
        self.assertEqual(snapshot.market_id, "0xabc123")
        self.assertEqual(snapshot.canonical_probabilities()["cut"], 0.27)
        self.assertEqual(snapshot.outcomes[1].label, "No")

    def test_kalshi_event_client_expands_event_markets(self) -> None:
        client = KalshiEventClient(fetch_json=lambda _: self.kalshi_event_payload)
        snapshots = client.fetch_snapshots(MarketWatchConfig(provider="kalshi_event", market_ref="KXFEDDECISION-26SEP"))
        self.assertEqual(len(snapshots), 2)
        hold_snapshot = next(snapshot for snapshot in snapshots if snapshot.market_id.endswith("-H0"))
        raise_snapshot = next(snapshot for snapshot in snapshots if snapshot.market_id.endswith("-H25"))
        self.assertEqual(hold_snapshot.target_meeting, "2026-09-16")
        self.assertEqual(hold_snapshot.canonical_probabilities()["hold"], 0.42)
        self.assertEqual(raise_snapshot.canonical_probabilities()["raise"], 0.54)

    def test_polymarket_event_client_expands_event_markets(self) -> None:
        client = PolymarketEventClient(fetch_json=lambda _: self.polymarket_event_payload)
        snapshots = client.fetch_snapshots(
            MarketWatchConfig(provider="polymarket_event", market_ref="fed-decision-in-september-762")
        )
        self.assertEqual(len(snapshots), 2)
        hold_snapshot = next(snapshot for snapshot in snapshots if snapshot.market_id == "0xa1")
        raise_snapshot = next(snapshot for snapshot in snapshots if snapshot.market_id == "0xa2")
        self.assertEqual(hold_snapshot.canonical_probabilities()["hold"], 0.4)
        self.assertEqual(raise_snapshot.canonical_probabilities()["raise"], 0.56)

    def test_store_round_trip_loads_documents(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MarketSnapshotStore(Path(temp_dir) / "snapshots.db")
            service = MarketIngestionService(
                store=store,
                kalshi_client=KalshiMarketClient(fetch_json=lambda _: self.kalshi_payload),
                polymarket_client=PolymarketMarketClient(fetch_json=lambda _: self.polymarket_payload),
            )
            service.ingest(
                [
                    MarketWatchConfig(
                        provider="kalshi",
                        market_ref="FEDSEP2026-HOLD",
                        target_meeting="2026-09-16",
                        outcome_mappings={"yes": "hold"},
                    ),
                    MarketWatchConfig(
                        provider="polymarket",
                        market_ref="fed-cut-by-september-2026",
                        target_meeting="2026-09-16",
                        outcome_mappings={"yes": "cut"},
                    ),
                ]
            )

            documents = store.load_documents(target_meeting="2026-09-16")
            self.assertEqual(len(documents), 2)
            self.assertTrue(all(document.kind.endswith("_market") for document in documents))
            self.assertTrue(any("Canonical probabilities" in document.content for document in documents))

    def test_service_ingests_event_watch_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MarketSnapshotStore(Path(temp_dir) / "snapshots.db")
            service = MarketIngestionService(
                store=store,
                kalshi_client=KalshiMarketClient(fetch_json=lambda _: self.kalshi_payload),
                polymarket_client=PolymarketMarketClient(fetch_json=lambda _: self.polymarket_payload),
            )
            service.clients["kalshi_event"] = KalshiEventClient(fetch_json=lambda _: self.kalshi_event_payload)
            service.clients["polymarket_event"] = PolymarketEventClient(
                fetch_json=lambda _: self.polymarket_event_payload
            )

            snapshots = service.ingest(
                [
                    MarketWatchConfig(provider="kalshi_event", market_ref="KXFEDDECISION-26SEP"),
                    MarketWatchConfig(provider="polymarket_event", market_ref="fed-decision-in-september-762"),
                ]
            )
            self.assertEqual(len(snapshots), 4)
            documents = store.load_documents(target_meeting="2026-09-16", limit=10)
            self.assertEqual(len(documents), 4)

    def test_load_watchlist_from_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            watchlist_path = Path(temp_dir) / "watchlist.json"
            watchlist_path.write_text(
                """
                [
                  {
                    "provider": "kalshi_event",
                    "market_ref": "KXFEDDECISION-26SEP",
                    "target_meeting": "2026-09-16"
                  }
                ]
                """.strip(),
                encoding="utf-8",
            )
            watchlist = load_watchlist(watchlist_path)
            self.assertEqual(len(watchlist), 1)
            self.assertEqual(watchlist[0].provider, "kalshi_event")
            self.assertEqual(watchlist[0].market_ref, "KXFEDDECISION-26SEP")


if __name__ == "__main__":
    unittest.main()
