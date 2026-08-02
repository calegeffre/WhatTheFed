import json
import tempfile
import unittest
from pathlib import Path

from whatthefed.fiscal_ingestion import (
    FiscalIngestionService,
    FiscalStore,
    TreasuryFiscalDataClient,
    build_dashboard_fiscal_payload,
    build_fiscal_bias_history,
    build_fiscal_knowledge_graph_payload,
    export_dashboard_fiscal_js,
    fiscal_bias,
)


def _report(report_date: str, fiscal_year: int, values: list[tuple[str, float, float]]) -> list[dict]:
    parent = f"fy-{fiscal_year}-{report_date}"
    rows = [
        {
            "record_date": report_date,
            "classification_id": parent,
            "classification_desc": f"FY {fiscal_year}",
            "record_type_cd": "SL",
            "sequence_number_cd": "2",
            "record_fiscal_year": str(fiscal_year),
        }
    ]
    for index, (month, receipts, outlays) in enumerate(values, start=1):
        rows.append(
            {
                "record_date": report_date,
                "parent_id": parent,
                "classification_desc": month,
                "record_type_cd": "MTH",
                "sequence_number_cd": f"2.{index}",
                "record_fiscal_year": str(fiscal_year),
                "current_month_gross_rcpt_amt": str(receipts),
                "current_month_gross_outly_amt": str(outlays),
                "current_month_dfct_sur_amt": str(outlays - receipts),
            }
        )
    return rows


SAMPLE_ROWS = (
    _report("2025-06-30", 2025, [("June", 400e9, 500e9)])
    + _report("2026-06-30", 2026, [("June", 420e9, 570e9)])
)
SAMPLE = {"data": SAMPLE_ROWS}


class FiscalTests(unittest.TestCase):
    def test_client_parses_current_fiscal_year_rows(self) -> None:
        client = TreasuryFiscalDataClient(get_json=lambda _url: SAMPLE)
        observations, url = client.fetch_observations(start_date="2025-01-01")
        self.assertEqual([item.observation_date for item in observations], ["2025-06-01", "2026-06-01"])
        self.assertIn("record_date", url)

    def test_bias_maps_deficit_expansion_hawkish(self) -> None:
        self.assertGreater(fiscal_bias(deficit=150, prior_deficit=100, prior_outlays=500), 0)
        self.assertLess(fiscal_bias(deficit=50, prior_deficit=100, prior_outlays=500), 0)

    def test_ingest_dashboard_graph_and_export(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "fiscal.db"
            service = FiscalIngestionService(
                FiscalStore(db_path), TreasuryFiscalDataClient(get_json=lambda _url: SAMPLE)
            )
            result = service.ingest(start_date="2025-01-01")
            self.assertEqual(result["observation_count"], 2)
            history = build_fiscal_bias_history(db_path=db_path)
            self.assertEqual(len(history), 1)
            self.assertGreater(history[0]["bias"], 0)
            payload = build_dashboard_fiscal_payload(db_path=db_path)
            self.assertIsNotNone(payload)
            assert payload is not None
            self.assertEqual(payload["heat_card"]["label"], "Fiscal Impulse")
            graph = build_fiscal_knowledge_graph_payload(db_path=db_path)
            self.assertEqual(graph["fiscal_graph"]["stats"]["series_count"], 3)

            output = Path(temp) / "fiscal.js"
            export_dashboard_fiscal_js(db_path=db_path, output_js_path=output)
            text = output.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("window.__FISCAL_DASHBOARD_DATA__ = "))
            self.assertIn("fiscal_graph", json.loads(text.split("=", 1)[1].strip().rstrip(";")))

    def test_client_rejects_empty_payload(self) -> None:
        client = TreasuryFiscalDataClient(get_json=lambda _url: {"data": []})
        with self.assertRaises(RuntimeError):
            client.fetch_observations(start_date="2025-01-01")


if __name__ == "__main__":
    unittest.main()
