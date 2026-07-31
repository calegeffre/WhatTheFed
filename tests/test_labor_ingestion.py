import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from whatthefed.labor_ingestion import (
    BLSLaborClient,
    LaborIngestionService,
    LaborStore,
    build_dashboard_labor_payload,
    build_labor_knowledge_graph_payload,
    export_dashboard_labor_js,
    export_dashboard_labor_kg_js,
)


SAMPLE_BLS_LABOR_PAYLOAD = {
    "status": "REQUEST_SUCCEEDED",
    "Results": {
        "series": [
            {
                "seriesID": "LNS14000000",
                "data": [
                    {"year": "2026", "period": "M03", "periodName": "March", "value": "4.1", "footnotes": [{}]},
                    {"year": "2026", "period": "M02", "periodName": "February", "value": "4.2", "footnotes": [{}]},
                    {"year": "2026", "period": "M01", "periodName": "January", "value": "4.3", "footnotes": [{}]},
                    {"year": "2025", "period": "M03", "periodName": "March", "value": "4.0", "footnotes": [{}]},
                    {"year": "2025", "period": "M02", "periodName": "February", "value": "4.1", "footnotes": [{}]},
                    {"year": "2025", "period": "M01", "periodName": "January", "value": "4.1", "footnotes": [{}]},
                ],
            },
            {
                "seriesID": "LNS11300000",
                "data": [
                    {"year": "2026", "period": "M03", "periodName": "March", "value": "62.1", "footnotes": [{}]},
                    {"year": "2026", "period": "M02", "periodName": "February", "value": "62.0", "footnotes": [{}]},
                    {"year": "2026", "period": "M01", "periodName": "January", "value": "61.9", "footnotes": [{}]},
                    {"year": "2025", "period": "M03", "periodName": "March", "value": "62.5", "footnotes": [{}]},
                    {"year": "2025", "period": "M02", "periodName": "February", "value": "62.4", "footnotes": [{}]},
                    {"year": "2025", "period": "M01", "periodName": "January", "value": "62.4", "footnotes": [{}]},
                ],
            },
            {
                "seriesID": "CES0000000001",
                "data": [
                    {"year": "2026", "period": "M03", "periodName": "March", "value": "158810", "footnotes": [{}]},
                    {"year": "2026", "period": "M02", "periodName": "February", "value": "158640", "footnotes": [{}]},
                    {"year": "2026", "period": "M01", "periodName": "January", "value": "158500", "footnotes": [{}]},
                    {"year": "2025", "period": "M03", "periodName": "March", "value": "156220", "footnotes": [{}]},
                    {"year": "2025", "period": "M02", "periodName": "February", "value": "156100", "footnotes": [{}]},
                    {"year": "2025", "period": "M01", "periodName": "January", "value": "156000", "footnotes": [{}]},
                ],
            },
            {
                "seriesID": "CES0500000003",
                "data": [
                    {"year": "2026", "period": "M03", "periodName": "March", "value": "35.35", "footnotes": [{}]},
                    {"year": "2026", "period": "M02", "periodName": "February", "value": "35.20", "footnotes": [{}]},
                    {"year": "2026", "period": "M01", "periodName": "January", "value": "35.10", "footnotes": [{}]},
                    {"year": "2025", "period": "M03", "periodName": "March", "value": "34.20", "footnotes": [{}]},
                    {"year": "2025", "period": "M02", "periodName": "February", "value": "34.10", "footnotes": [{}]},
                    {"year": "2025", "period": "M01", "periodName": "January", "value": "34.00", "footnotes": [{}]},
                ],
            },
            {
                "seriesID": "LNS13000000",
                "data": [
                    {"year": "2026", "period": "M03", "periodName": "March", "value": "6900", "footnotes": [{}]},
                    {"year": "2026", "period": "M02", "periodName": "February", "value": "6950", "footnotes": [{}]},
                    {"year": "2026", "period": "M01", "periodName": "January", "value": "7000", "footnotes": [{}]},
                    {"year": "2025", "period": "M03", "periodName": "March", "value": "6700", "footnotes": [{}]},
                    {"year": "2025", "period": "M02", "periodName": "February", "value": "6750", "footnotes": [{}]},
                    {"year": "2025", "period": "M01", "periodName": "January", "value": "6800", "footnotes": [{}]},
                ],
            },
            {
                "seriesID": "JTS000000000000000JOL",
                "data": [
                    {"year": "2026", "period": "M03", "periodName": "March", "value": "7500", "footnotes": [{}]},
                    {"year": "2026", "period": "M02", "periodName": "February", "value": "7550", "footnotes": [{}]},
                    {"year": "2026", "period": "M01", "periodName": "January", "value": "7600", "footnotes": [{}]},
                    {"year": "2025", "period": "M03", "periodName": "March", "value": "8200", "footnotes": [{}]},
                    {"year": "2025", "period": "M02", "periodName": "February", "value": "8250", "footnotes": [{}]},
                    {"year": "2025", "period": "M01", "periodName": "January", "value": "8300", "footnotes": [{}]},
                ],
            },
            {
                "seriesID": "JTS000000000000000QUL",
                "data": [
                    {"year": "2026", "period": "M03", "periodName": "March", "value": "3000", "footnotes": [{}]},
                    {"year": "2026", "period": "M02", "periodName": "February", "value": "3050", "footnotes": [{}]},
                    {"year": "2026", "period": "M01", "periodName": "January", "value": "3100", "footnotes": [{}]},
                    {"year": "2025", "period": "M03", "periodName": "March", "value": "3300", "footnotes": [{}]},
                    {"year": "2025", "period": "M02", "periodName": "February", "value": "3350", "footnotes": [{}]},
                    {"year": "2025", "period": "M01", "periodName": "January", "value": "3400", "footnotes": [{}]},
                ],
            },
            {
                "seriesID": "JTS000000000000000HIL",
                "data": [
                    {"year": "2026", "period": "M03", "periodName": "March", "value": "5100", "footnotes": [{}]},
                    {"year": "2026", "period": "M02", "periodName": "February", "value": "5150", "footnotes": [{}]},
                    {"year": "2026", "period": "M01", "periodName": "January", "value": "5200", "footnotes": [{}]},
                    {"year": "2025", "period": "M03", "periodName": "March", "value": "5500", "footnotes": [{}]},
                    {"year": "2025", "period": "M02", "periodName": "February", "value": "5550", "footnotes": [{}]},
                    {"year": "2025", "period": "M01", "periodName": "January", "value": "5600", "footnotes": [{}]},
                ],
            },
            {
                "seriesID": "LNS12300060",
                "data": [
                    {"year": "2026", "period": "M03", "periodName": "March", "value": "80.1", "footnotes": [{}]},
                    {"year": "2026", "period": "M02", "periodName": "February", "value": "80.0", "footnotes": [{}]},
                    {"year": "2026", "period": "M01", "periodName": "January", "value": "79.9", "footnotes": [{}]},
                    {"year": "2025", "period": "M03", "periodName": "March", "value": "79.6", "footnotes": [{}]},
                    {"year": "2025", "period": "M02", "periodName": "February", "value": "79.5", "footnotes": [{}]},
                    {"year": "2025", "period": "M01", "periodName": "January", "value": "79.4", "footnotes": [{}]},
                ],
            },
        ]
    },
}


class LaborIngestionTests(unittest.TestCase):
    def test_client_parses_monthly_observations(self) -> None:
        client = BLSLaborClient(post_json=lambda *_args, **_kwargs: SAMPLE_BLS_LABOR_PAYLOAD)
        data = client.fetch_observations(series_ids=["LNS14000000"], start_year=2025, end_year=2026)
        self.assertIn("LNS14000000", data)
        self.assertEqual(len(data["LNS14000000"]), 6)
        self.assertEqual(data["LNS14000000"][-1].observation_date, "2026-03-01")

    def test_service_writes_observations_and_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "labor.db"
            service = LaborIngestionService(
                store=LaborStore(db_path),
                client=BLSLaborClient(post_json=lambda *_args, **_kwargs: SAMPLE_BLS_LABOR_PAYLOAD),
            )
            result = service.ingest(start_year=2025, end_year=2026)
            self.assertGreater(result["observation_count"], 0)
            self.assertEqual(result["metric_date"], "2026-03-01")

            payload = build_dashboard_labor_payload(db_path=db_path)
            self.assertIsNotNone(payload)
            assert payload is not None
            self.assertEqual(payload["heat_card"]["label"], "Labor Momentum")
            self.assertIn("labor_bias", payload["metrics"])

            docs = LaborStore(db_path).load_documents(per_series_limit=2)
            self.assertGreaterEqual(len(docs), 2)
            self.assertTrue(all(doc.kind == "labor_observation" for doc in docs))

            connection = sqlite3.connect(db_path)
            try:
                count = connection.execute("SELECT COUNT(*) FROM labor_metrics").fetchone()[0]
            finally:
                connection.close()
            self.assertGreater(count, 0)

    def test_exports_dashboard_and_kg_js(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "labor.db"
            labor_js = Path(temp_dir) / "labor_dashboard_data.js"
            kg_js = Path(temp_dir) / "labor_kg_dashboard_data.js"
            service = LaborIngestionService(
                store=LaborStore(db_path),
                client=BLSLaborClient(post_json=lambda *_args, **_kwargs: SAMPLE_BLS_LABOR_PAYLOAD),
            )
            service.ingest(start_year=2025, end_year=2026)

            labor_payload = export_dashboard_labor_js(db_path=db_path, output_js_path=labor_js)
            kg_payload = export_dashboard_labor_kg_js(db_path=db_path, output_js_path=kg_js, months=3)
            self.assertIsNotNone(labor_payload)
            self.assertIsNotNone(kg_payload)

            labor_text = labor_js.read_text(encoding="utf-8")
            self.assertTrue(labor_text.startswith("window.__LABOR_DASHBOARD_DATA__ = "))
            parsed_labor = json.loads(labor_text.split("=", 1)[1].strip().rstrip(";"))
            self.assertIn("heat_card", parsed_labor)

            kg_text = kg_js.read_text(encoding="utf-8")
            self.assertTrue(kg_text.startswith("window.__LABOR_KG_DASHBOARD_DATA__ = "))
            parsed_kg = json.loads(kg_text.split("=", 1)[1].strip().rstrip(";"))
            self.assertIn("labor_graph", parsed_kg)
            self.assertGreater(parsed_kg["labor_graph"]["stats"]["observation_count"], 0)

    def test_build_labor_knowledge_graph_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "labor.db"
            service = LaborIngestionService(
                store=LaborStore(db_path),
                client=BLSLaborClient(post_json=lambda *_args, **_kwargs: SAMPLE_BLS_LABOR_PAYLOAD),
            )
            service.ingest(start_year=2025, end_year=2026)

            payload = build_labor_knowledge_graph_payload(db_path=db_path, months=2)
            self.assertIsNotNone(payload)
            assert payload is not None
            graph = payload["labor_graph"]
            self.assertGreater(graph["stats"]["series_count"], 0)
            self.assertGreater(graph["stats"]["observation_count"], 0)
            self.assertGreater(graph["stats"]["edge_count"], 0)


if __name__ == "__main__":
    unittest.main()
