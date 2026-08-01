import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from whatthefed.breakeven_ingestion import (
    NEUTRAL_BREAKEVEN_PCT,
    PROVIDER,
    BreakevenIngestionService,
    RealYieldStore,
    TreasuryRealYieldClient,
    breakeven_bias,
    build_breakeven_bias_history,
    build_breakeven_knowledge_graph_payload,
    build_breakeven_series,
    build_dashboard_breakeven_payload,
    export_dashboard_breakeven_js,
)
from whatthefed.treasury_ingestion import (
    TreasuryIngestionService,
    TreasuryParYieldClient,
    TreasuryStore,
)


REAL_CSV = (
    'Date,"5 YR","7 YR","10 YR","20 YR","30 YR"\n'
    "07/31/2026,2.19,2.32,2.47,2.82,3.03\n"
    "07/30/2026,2.14,2.26,2.41,2.77,2.98\n"
)

NOMINAL_CSV = (
    'Date,"1 Mo","3 Mo","1 Yr","2 Yr","3 Yr","5 Yr","7 Yr","10 Yr","20 Yr","30 Yr"\n'
    "07/31/2026,4.35,4.32,4.27,4.28,4.35,4.48,4.62,4.75,5.05,5.20\n"
    "07/30/2026,4.34,4.31,4.25,4.24,4.31,4.44,4.58,4.70,5.01,5.16\n"
)


def _seed(db_path: Path) -> None:
    TreasuryIngestionService(
        store=TreasuryStore(db_path),
        client=TreasuryParYieldClient(get_text=lambda url: NOMINAL_CSV),
    ).ingest(years=[2026])
    BreakevenIngestionService(
        store=RealYieldStore(db_path),
        client=TreasuryRealYieldClient(get_text=lambda url: REAL_CSV),
    ).ingest(years=[2026])


class BreakevenIngestionTests(unittest.TestCase):
    def test_client_parses_uppercase_tenor_headers(self) -> None:
        client = TreasuryRealYieldClient(get_text=lambda url: REAL_CSV)
        snapshot_at, points, source_url = client.fetch_points(year=2026)
        self.assertEqual(snapshot_at, "2026-07-31")
        self.assertIn("daily_treasury_real_yield_curve", source_url)
        self.assertEqual(len(points), 10)

        latest = {point.symbol: point for point in points if point.snapshot_at == "2026-07-31"}
        self.assertEqual(latest["TIPS10Y"].real_yield_pct, 2.47)
        self.assertEqual(latest["TIPS10Y"].maturity, "10Y")
        self.assertEqual(latest["TIPS10Y"].provider, PROVIDER)
        self.assertAlmostEqual(latest["TIPS10Y"].change_value, 0.06, places=4)

    def test_breakeven_is_nominal_minus_real(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "breakeven.db"
            _seed(db_path)

            series = build_breakeven_series(db_path=db_path)
            # 4.75 nominal - 2.47 real
            self.assertAlmostEqual(series["2026-07-31"]["10Y"], 2.28, places=4)
            self.assertAlmostEqual(series["2026-07-31"]["5Y"], 2.29, places=4)
            self.assertAlmostEqual(series["2026-07-30"]["10Y"], 2.29, places=4)

    def test_breakeven_series_skips_unmatched_maturities(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "breakeven.db"
            _seed(db_path)
            series = build_breakeven_series(db_path=db_path)
            # 1 Mo / 3 Mo / 2 Yr have no TIPS counterpart and must not appear.
            self.assertEqual(sorted(series["2026-07-31"]), ["10Y", "20Y", "30Y", "5Y", "7Y"])

    def test_bias_scale_is_anchored_on_the_cpi_pce_wedge(self) -> None:
        self.assertEqual(breakeven_bias(NEUTRAL_BREAKEVEN_PCT), 0.0)
        self.assertEqual(breakeven_bias(2.80), 1.0)
        self.assertEqual(breakeven_bias(1.80), -1.0)
        self.assertEqual(breakeven_bias(4.00), 1.0)
        self.assertEqual(breakeven_bias(None), 0.0)
        self.assertGreater(breakeven_bias(2.50), 0.0)
        self.assertLess(breakeven_bias(2.10), 0.0)

    def test_history_and_dashboard_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "breakeven.db"
            _seed(db_path)

            history = build_breakeven_bias_history(db_path=db_path)
            self.assertEqual([entry["date"] for entry in history], ["2026-07-30", "2026-07-31"])
            self.assertEqual(history[-1]["bias"], breakeven_bias(2.28))

            payload = build_dashboard_breakeven_payload(db_path=db_path)
            self.assertIsNotNone(payload)
            assert payload is not None
            self.assertEqual(payload["metric_date"], "2026-07-31")
            self.assertAlmostEqual(payload["metrics"]["breakeven_10y"], 2.28, places=4)
            self.assertEqual(payload["heat_card"]["label"], "Inflation Expectations")
            self.assertEqual(len(payload["latest_values"]), 5)
            self.assertEqual(payload["latest_values"][0]["nominal_symbol"], "UST10Y")

    def test_exports_js_and_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "breakeven.db"
            output_js = Path(temp_dir) / "breakeven_dashboard_data.js"
            _seed(db_path)

            graph = build_breakeven_knowledge_graph_payload(db_path=db_path)
            self.assertIsNotNone(graph)
            assert graph is not None
            self.assertGreater(graph["breakeven_graph"]["stats"]["observation_count"], 0)

            exported = export_dashboard_breakeven_js(db_path=db_path, output_js_path=output_js)
            self.assertIsNotNone(exported)
            text = output_js.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("window.__BREAKEVEN_DASHBOARD_DATA__ = "))
            parsed = json.loads(text.split("=", 1)[1].strip().rstrip(";"))
            self.assertIn("breakeven_graph", parsed)
            self.assertIn("bias_history", parsed)

    def test_returns_none_without_nominal_curve(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "real_only.db"
            BreakevenIngestionService(
                store=RealYieldStore(db_path),
                client=TreasuryRealYieldClient(get_text=lambda url: REAL_CSV),
            ).ingest(years=[2026])
            # Nominal table must exist but stay empty for the join to yield nothing.
            connection = sqlite3.connect(db_path)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS treasury_observations (
                    snapshot_at TEXT NOT NULL, symbol TEXT NOT NULL, label TEXT NOT NULL,
                    maturity TEXT NOT NULL, yield_pct REAL, price REAL, change_value REAL,
                    source_url TEXT NOT NULL, provider TEXT NOT NULL, fetched_at TEXT NOT NULL,
                    PRIMARY KEY (snapshot_at, symbol)
                )
                """
            )
            connection.commit()
            connection.close()
            self.assertEqual(build_breakeven_series(db_path=db_path), {})
            self.assertIsNone(build_dashboard_breakeven_payload(db_path=db_path))


if __name__ == "__main__":
    unittest.main()
