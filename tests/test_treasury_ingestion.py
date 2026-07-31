import json
import tempfile
import unittest
from pathlib import Path

from whatthefed.treasury_ingestion import (
    PROVIDER,
    TreasuryIngestionService,
    TreasuryParYieldClient,
    TreasuryStore,
    build_treasury_dashboard_payload,
    build_treasury_knowledge_graph_payload,
    build_treasury_slope_history,
    export_dashboard_treasury_js,
    treasury_slope_bias,
)


SAMPLE_CSV = (
    'Date,"1 Mo","1.5 Month","2 Mo","3 Mo","4 Mo","6 Mo","1 Yr","2 Yr","3 Yr",'
    '"5 Yr","7 Yr","10 Yr","20 Yr","30 Yr"\n'
    "07/31/2026,4.35,4.34,4.33,4.32,4.30,4.29,4.27,4.28,4.35,4.48,4.62,4.75,5.05,5.20\n"
    "07/30/2026,4.34,4.33,4.32,4.31,4.29,4.28,4.25,4.24,4.31,4.44,4.58,4.70,5.01,5.16\n"
    "07/29/2026,4.33,4.32,4.31,4.30,4.28,4.26,4.22,4.20,4.27,4.40,4.54,4.66,4.97,5.12\n"
)


def _client() -> TreasuryParYieldClient:
    return TreasuryParYieldClient(get_text=lambda url: SAMPLE_CSV)


class TreasuryIngestionTests(unittest.TestCase):
    def test_client_parses_daily_par_yield_csv(self) -> None:
        snapshot_at, points, source_url = _client().fetch_points(year=2026)
        self.assertEqual(snapshot_at, "2026-07-31")
        self.assertIn("daily_treasury_yield_curve", source_url)
        self.assertEqual(len(points), 42)

        latest = {point.symbol: point for point in points if point.snapshot_at == "2026-07-31"}
        self.assertEqual(latest["UST10Y"].yield_pct, 4.75)
        self.assertEqual(latest["UST10Y"].maturity, "10Y")
        self.assertEqual(latest["UST10Y"].provider, PROVIDER)
        # 4.75 - 4.70 day-over-day
        self.assertAlmostEqual(latest["UST10Y"].change_value, 0.05, places=4)
        # The odd "1.5 Month" column must not be misread as a 5-month tenor.
        self.assertEqual(latest["UST1_5M"].maturity, "1.5M")
        self.assertIsNone(
            next(p for p in points if p.snapshot_at == "2026-07-29" and p.symbol == "UST10Y").change_value
        )

    def test_client_sorts_points_by_date_then_maturity(self) -> None:
        _snapshot_at, points, _url = _client().fetch_points(year=2026)
        first_day = [point.maturity for point in points if point.snapshot_at == "2026-07-29"]
        self.assertEqual(first_day[:4], ["1M", "1.5M", "2M", "3M"])
        self.assertEqual(first_day[-1], "30Y")

    def test_service_persists_points_and_documents(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "treasury.db"
            service = TreasuryIngestionService(store=TreasuryStore(db_path), client=_client())
            result = service.ingest(years=[2026])
            self.assertEqual(result["observation_count"], 42)
            self.assertEqual(result["snapshot_at"], "2026-07-31")
            self.assertEqual(result["years"], [2026])

            # Ingestion must be idempotent on (snapshot_at, symbol).
            service.ingest(years=[2026])
            docs = TreasuryStore(db_path).load_documents(per_symbol_limit=2)
            self.assertEqual(len(docs), 28)
            self.assertTrue(all(doc.kind == "treasury_observation" for doc in docs))

    def test_slope_history_tracks_ten_minus_two(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "treasury.db"
            TreasuryIngestionService(store=TreasuryStore(db_path), client=_client()).ingest(years=[2026])

            history = build_treasury_slope_history(db_path=db_path)
            self.assertEqual([entry["date"] for entry in history], ["2026-07-29", "2026-07-30", "2026-07-31"])
            self.assertAlmostEqual(history[-1]["slope"], 0.47, places=4)
            self.assertEqual(history[-1]["bias"], treasury_slope_bias(0.47))

    def test_slope_bias_scale(self) -> None:
        self.assertEqual(treasury_slope_bias(1.0), 0.0)
        self.assertEqual(treasury_slope_bias(0.0), 1.0)
        self.assertEqual(treasury_slope_bias(2.0), -1.0)
        self.assertEqual(treasury_slope_bias(5.0), -1.0)

    def test_exports_dashboard_and_graph_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "treasury.db"
            output_js = Path(temp_dir) / "treasury_dashboard_data.js"
            TreasuryIngestionService(store=TreasuryStore(db_path), client=_client()).ingest(years=[2026])

            dashboard = build_treasury_dashboard_payload(db_path=db_path, row_limit=20)
            self.assertIsNotNone(dashboard)
            assert dashboard is not None
            self.assertEqual(dashboard["provider"], PROVIDER)
            self.assertEqual(len(dashboard["points"]), 14)
            self.assertEqual(len(dashboard["slope_history"]), 3)

            kg_payload = build_treasury_knowledge_graph_payload(db_path=db_path, per_symbol_limit=4)
            self.assertIsNotNone(kg_payload)
            assert kg_payload is not None
            self.assertGreater(kg_payload["treasury_graph"]["stats"]["observation_count"], 0)

            exported = export_dashboard_treasury_js(db_path=db_path, output_js_path=output_js)
            self.assertIsNotNone(exported)
            text = output_js.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("window.__TREASURY_DASHBOARD_DATA__ = "))
            parsed = json.loads(text.split("=", 1)[1].strip().rstrip(";"))
            self.assertIn("treasury_graph", parsed)
            self.assertIn("slope_history", parsed)


if __name__ == "__main__":
    unittest.main()
