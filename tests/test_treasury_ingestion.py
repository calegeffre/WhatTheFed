import json
import tempfile
import unittest
from pathlib import Path

from whatthefed.treasury_ingestion import (
    BloombergTreasuryClient,
    TreasuryIngestionService,
    TreasuryStore,
    build_treasury_dashboard_payload,
    build_treasury_knowledge_graph_payload,
    export_dashboard_treasury_js,
)


SAMPLE_TREASURY_PAYLOAD = {
    "snapshot_at": "2026-07-31T13:30:00Z",
    "source_url": "https://www.bloomberg.com/markets/rates-bonds/government-bonds/us",
    "points": [
        {
            "symbol": "USGG2YR:IND",
            "label": "US Treasury 2Y",
            "maturity": "2Y",
            "yield_pct": 3.81,
            "price": 99.17,
            "change_value": -0.03,
        },
        {
            "symbol": "USGG5YR:IND",
            "label": "US Treasury 5Y",
            "maturity": "5Y",
            "yield_pct": 3.74,
            "price": 98.56,
            "change_value": -0.04,
        },
        {
            "symbol": "USGG10YR:IND",
            "label": "US Treasury 10Y",
            "maturity": "10Y",
            "yield_pct": 3.89,
            "price": 96.84,
            "change_value": -0.06,
        },
    ],
}


class TreasuryIngestionTests(unittest.TestCase):
    def test_client_loads_points_from_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "treasury.json"
            input_path.write_text(json.dumps(SAMPLE_TREASURY_PAYLOAD), encoding="utf-8")
            client = BloombergTreasuryClient()
            snapshot_at, points, source_url = client.fetch_points(input_json_path=input_path)
            self.assertEqual(snapshot_at, "2026-07-31T13:30:00Z")
            self.assertEqual(source_url, SAMPLE_TREASURY_PAYLOAD["source_url"])
            self.assertEqual(len(points), 3)
            self.assertEqual(points[0].symbol, "USGG2YR:IND")

    def test_service_persists_points_and_documents(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "treasury.db"
            input_path = Path(temp_dir) / "treasury.json"
            input_path.write_text(json.dumps(SAMPLE_TREASURY_PAYLOAD), encoding="utf-8")

            service = TreasuryIngestionService(store=TreasuryStore(db_path))
            result = service.ingest(input_json_path=input_path)
            self.assertEqual(result["observation_count"], 3)
            self.assertEqual(result["snapshot_at"], "2026-07-31T13:30:00Z")

            docs = TreasuryStore(db_path).load_documents(per_symbol_limit=2)
            self.assertEqual(len(docs), 3)
            self.assertTrue(all(doc.kind == "treasury_observation" for doc in docs))

    def test_exports_dashboard_and_graph_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "treasury.db"
            input_path = Path(temp_dir) / "treasury.json"
            output_js = Path(temp_dir) / "treasury_dashboard_data.js"
            input_path.write_text(json.dumps(SAMPLE_TREASURY_PAYLOAD), encoding="utf-8")

            service = TreasuryIngestionService(store=TreasuryStore(db_path))
            service.ingest(input_json_path=input_path)

            dashboard = build_treasury_dashboard_payload(db_path=db_path, row_limit=20)
            self.assertIsNotNone(dashboard)
            assert dashboard is not None
            self.assertEqual(dashboard["provider"], "Bloomberg")
            self.assertEqual(len(dashboard["points"]), 3)

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


if __name__ == "__main__":
    unittest.main()
