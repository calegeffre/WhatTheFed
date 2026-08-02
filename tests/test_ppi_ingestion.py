import json
import tempfile
import unittest
from pathlib import Path

from whatthefed.ppi_ingestion import (
    BLSPPIClient,
    PPIIngestionService,
    PPIStore,
    build_dashboard_ppi_payload,
    build_ppi_bias_history,
    build_ppi_knowledge_graph_payload,
    export_dashboard_ppi_js,
    ppi_bias,
)


def _series(series_id: str, values: list[tuple[int, int, float]]) -> dict:
    return {
        "seriesID": series_id,
        "data": [
            {
                "year": str(year),
                "period": f"M{month:02d}",
                "periodName": "Month",
                "value": str(value),
                "footnotes": [{"code": "P"}] if index == 0 else [{}],
            }
            for index, (year, month, value) in enumerate(reversed(values))
        ],
    }


DATES = [(2025, month, 100 + month) for month in range(1, 13)] + [
    (2026, month, 104 + month) for month in range(1, 7)
]
SAMPLE = {
    "status": "REQUEST_SUCCEEDED",
    "Results": {
        "series": [
            _series("WPSFD4", DATES),
            _series("WPSFD49116", [(year, month, value * 1.01) for year, month, value in DATES]),
            _series("WPSFD411", DATES),
            _series("WPSFD412", DATES),
            _series("WPSFD413", DATES),
        ]
    },
}


class PPITests(unittest.TestCase):
    def test_client_parses_monthly_points_and_preliminary_flag(self) -> None:
        client = BLSPPIClient(post_json=lambda *_args, **_kwargs: SAMPLE)
        result = client.fetch_observations(series_ids=["WPSFD4"], start_year=2025, end_year=2026)
        self.assertEqual(len(result["WPSFD4"]), 18)
        self.assertTrue(result["WPSFD4"][-1].preliminary)

    def test_bias_is_hawkish_above_neutral(self) -> None:
        self.assertGreater(ppi_bias(headline_yoy=4, core_yoy=3, core_3m=3.5), 0)
        self.assertLess(ppi_bias(headline_yoy=0, core_yoy=1, core_3m=1), 0)

    def test_ingest_dashboard_graph_and_export(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "ppi.db"
            service = PPIIngestionService(
                PPIStore(db_path), BLSPPIClient(post_json=lambda *_args, **_kwargs: SAMPLE)
            )
            result = service.ingest(start_year=2025, end_year=2026)
            self.assertEqual(result["observation_count"], 90)
            history = build_ppi_bias_history(db_path=db_path)
            self.assertGreaterEqual(len(history), 6)
            payload = build_dashboard_ppi_payload(db_path=db_path)
            self.assertIsNotNone(payload)
            assert payload is not None
            self.assertEqual(payload["heat_card"]["label"], "Producer Prices")
            graph = build_ppi_knowledge_graph_payload(db_path=db_path)
            self.assertEqual(graph["ppi_graph"]["stats"]["series_count"], 5)

            output = Path(temp) / "ppi.js"
            export_dashboard_ppi_js(db_path=db_path, output_js_path=output)
            text = output.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("window.__PPI_DASHBOARD_DATA__ = "))
            self.assertIn("ppi_graph", json.loads(text.split("=", 1)[1].strip().rstrip(";")))

    def test_client_rejects_failed_response(self) -> None:
        client = BLSPPIClient(post_json=lambda *_args, **_kwargs: {"status": "REQUEST_FAILED"})
        with self.assertRaises(RuntimeError):
            client.fetch_observations(series_ids=["WPSFD4"], start_year=2025, end_year=2026)


if __name__ == "__main__":
    unittest.main()
