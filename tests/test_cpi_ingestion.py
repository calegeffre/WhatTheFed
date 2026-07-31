import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from whatthefed.cpi_ingestion import (
    BLSCPIClient,
    CPIIngestionService,
    CPIStore,
    build_cpi_knowledge_graph_payload,
    build_dashboard_cpi_payload,
    export_dashboard_cpi_js,
    export_dashboard_kg_js,
)


SAMPLE_BLS_PAYLOAD = {
    "status": "REQUEST_SUCCEEDED",
    "Results": {
        "series": [
            {
                "seriesID": "CUSR0000SA0",
                "data": [
                    {"year": "2026", "period": "M03", "periodName": "March", "value": "330.293", "footnotes": [{}]},
                    {"year": "2026", "period": "M02", "periodName": "February", "value": "327.460", "footnotes": [{}]},
                    {"year": "2026", "period": "M01", "periodName": "January", "value": "326.588", "footnotes": [{}]},
                    {"year": "2025", "period": "M03", "periodName": "March", "value": "319.785", "footnotes": [{}]},
                    {"year": "2025", "period": "M02", "periodName": "February", "value": "319.679", "footnotes": [{}]},
                    {"year": "2025", "period": "M01", "periodName": "January", "value": "318.961", "footnotes": [{}]},
                    {"year": "2025", "period": "M10", "periodName": "October", "value": "-", "footnotes": [{"text": "missing"}]},
                ],
            },
            {
                "seriesID": "CUSR0000SA0L1E",
                "data": [
                    {"year": "2026", "period": "M03", "periodName": "March", "value": "334.165", "footnotes": [{}]},
                    {"year": "2026", "period": "M02", "periodName": "February", "value": "333.512", "footnotes": [{}]},
                    {"year": "2026", "period": "M01", "periodName": "January", "value": "332.793", "footnotes": [{}]},
                    {"year": "2025", "period": "M03", "periodName": "March", "value": "325.690", "footnotes": [{}]},
                    {"year": "2025", "period": "M02", "periodName": "February", "value": "325.465", "footnotes": [{}]},
                    {"year": "2025", "period": "M01", "periodName": "January", "value": "324.638", "footnotes": [{}]},
                ],
            },
        ]
    },
}


class CPIIngestionTests(unittest.TestCase):
    def test_client_parses_monthly_observations(self) -> None:
        client = BLSCPIClient(post_json=lambda *_args, **_kwargs: SAMPLE_BLS_PAYLOAD)
        data = client.fetch_observations(series_ids=["CUSR0000SA0"], start_year=2025, end_year=2026)
        self.assertIn("CUSR0000SA0", data)
        # One missing "-" value should be dropped.
        self.assertEqual(len(data["CUSR0000SA0"]), 6)
        self.assertEqual(data["CUSR0000SA0"][-1].observation_date, "2026-03-01")

    def test_service_writes_observations_and_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "cpi.db"
            store = CPIStore(db_path)
            service = CPIIngestionService(
                store=store,
                client=BLSCPIClient(post_json=lambda *_args, **_kwargs: SAMPLE_BLS_PAYLOAD),
            )
            result = service.ingest(start_year=2025, end_year=2026)
            self.assertGreater(result["observation_count"], 0)
            self.assertIsNotNone(result["metric_date"])

            payload = build_dashboard_cpi_payload(db_path=db_path)
            self.assertIsNotNone(payload)
            assert payload is not None
            self.assertIn("heat_card", payload)
            self.assertIn("cpi_bias", payload["metrics"])
            self.assertEqual(payload["heat_card"]["label"], "CPI Momentum")

            docs = store.load_documents(per_series_limit=2)
            self.assertGreaterEqual(len(docs), 2)
            self.assertTrue(all(doc.kind == "cpi_observation" for doc in docs))

            connection = sqlite3.connect(db_path)
            try:
                count = connection.execute("SELECT COUNT(*) FROM cpi_metrics").fetchone()[0]
            finally:
                connection.close()
            self.assertGreater(count, 0)

    def test_exports_dashboard_and_kg_js(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "cpi.db"
            cpi_js = Path(temp_dir) / "cpi_dashboard_data.js"
            kg_js = Path(temp_dir) / "kg_dashboard_data.js"
            service = CPIIngestionService(
                store=CPIStore(db_path),
                client=BLSCPIClient(post_json=lambda *_args, **_kwargs: SAMPLE_BLS_PAYLOAD),
            )
            service.ingest(start_year=2025, end_year=2026)

            cpi_payload = export_dashboard_cpi_js(db_path=db_path, output_js_path=cpi_js)
            kg_payload = export_dashboard_kg_js(db_path=db_path, output_js_path=kg_js, months=3)
            self.assertIsNotNone(cpi_payload)
            self.assertIsNotNone(kg_payload)

            cpi_text = cpi_js.read_text(encoding="utf-8")
            self.assertTrue(cpi_text.startswith("window.__CPI_DASHBOARD_DATA__ = "))
            parsed_cpi = json.loads(cpi_text.split("=", 1)[1].strip().rstrip(";"))
            self.assertIn("heat_card", parsed_cpi)

            kg_text = kg_js.read_text(encoding="utf-8")
            self.assertTrue(kg_text.startswith("window.__KG_DASHBOARD_DATA__ = "))
            parsed_kg = json.loads(kg_text.split("=", 1)[1].strip().rstrip(";"))
            self.assertIn("cpi_graph", parsed_kg)
            self.assertGreater(parsed_kg["cpi_graph"]["stats"]["observation_count"], 0)

    def test_build_cpi_knowledge_graph_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "cpi.db"
            service = CPIIngestionService(
                store=CPIStore(db_path),
                client=BLSCPIClient(post_json=lambda *_args, **_kwargs: SAMPLE_BLS_PAYLOAD),
            )
            service.ingest(start_year=2025, end_year=2026)

            payload = build_cpi_knowledge_graph_payload(db_path=db_path, months=2)
            self.assertIsNotNone(payload)
            assert payload is not None
            graph = payload["cpi_graph"]
            self.assertGreater(graph["stats"]["series_count"], 0)
            self.assertGreater(graph["stats"]["observation_count"], 0)
            self.assertGreater(graph["stats"]["edge_count"], 0)


if __name__ == "__main__":
    unittest.main()
