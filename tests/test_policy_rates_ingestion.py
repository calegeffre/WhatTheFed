import json
import tempfile
import unittest
from pathlib import Path

from whatthefed.policy_rates_ingestion import (
    PROVIDER,
    NyFedReferenceRateClient,
    PolicyRateIngestionError,
    PolicyRateIngestionService,
    PolicyRateStore,
    build_dashboard_policy_rates_payload,
    build_policy_rate_bias_history,
    build_policy_rate_knowledge_graph_payload,
    export_dashboard_policy_rates_js,
    policy_rate_bias,
)


def _ref_rows(rate_type: str, rows: list[dict]) -> str:
    return json.dumps({"refRates": [{"type": rate_type, **row} for row in rows]})


SAMPLE = {
    "EFFR": _ref_rows(
        "EFFR",
        [
            {
                "effectiveDate": "2026-07-30",
                "percentRate": 3.63,
                "percentPercentile1": 3.60,
                "percentPercentile99": 3.68,
                "targetRateFrom": 3.50,
                "targetRateTo": 3.75,
                "volumeInBillions": 121,
            },
            {
                "effectiveDate": "2026-07-29",
                "percentRate": 3.62,
                "percentPercentile1": 3.59,
                "percentPercentile99": 3.67,
                "targetRateFrom": 3.50,
                "targetRateTo": 3.75,
                "volumeInBillions": 118,
            },
        ],
    ),
    "SOFR": _ref_rows(
        "SOFR",
        [
            {
                "effectiveDate": "2026-07-30",
                "percentRate": 3.65,
                "percentPercentile1": 3.60,
                "percentPercentile99": 3.73,
                "volumeInBillions": 3011,
            },
            {
                "effectiveDate": "2026-07-29",
                "percentRate": 3.64,
                "percentPercentile1": 3.60,
                "percentPercentile99": 3.71,
                "volumeInBillions": 2950,
            },
        ],
    ),
}


def _client() -> NyFedReferenceRateClient:
    def get_json(url: str) -> object:
        rate_type = "EFFR" if "effr" in url else "SOFR"
        return json.loads(SAMPLE[rate_type])

    return NyFedReferenceRateClient(get_json=get_json)


class PolicyRateIngestionTests(unittest.TestCase):
    def test_client_parses_reference_rates(self) -> None:
        latest, points, _url = _client().fetch_points(
            start_date="2026-07-01", end_date="2026-07-31", rate_types=["EFFR", "SOFR"]
        )
        self.assertEqual(latest, "2026-07-30")
        self.assertEqual(len(points), 4)

        effr = next(p for p in points if p.rate_type == "EFFR" and p.effective_date == "2026-07-30")
        self.assertEqual(effr.percent_rate, 3.63)
        self.assertEqual(effr.target_rate_from, 3.50)
        self.assertEqual(effr.target_rate_to, 3.75)
        self.assertEqual(effr.provider, PROVIDER)

    def test_client_rejects_unknown_rate_type(self) -> None:
        with self.assertRaises(PolicyRateIngestionError):
            _client().fetch_points(start_date="2026-07-01", end_date="2026-07-31", rate_types=["NOPE"])

    def test_client_builds_family_aware_urls(self) -> None:
        client = _client()
        self.assertIn("/unsecured/effr/search.json", client.build_url("EFFR", start_date="a", end_date="b"))
        self.assertIn("/secured/sofr/search.json", client.build_url("SOFR", start_date="a", end_date="b"))

    def test_bias_is_negative_when_funding_is_stressed(self) -> None:
        calm = policy_rate_bias(sofr_effr_spread_bps=0.0, repo_dispersion_bps=10.0, band_position=0.5)
        stressed = policy_rate_bias(sofr_effr_spread_bps=20.0, repo_dispersion_bps=30.0, band_position=0.95)
        self.assertEqual(calm, 0.0)
        self.assertLess(stressed, -0.9)
        # Funding stress must read dovish, never hawkish.
        self.assertLess(stressed, calm)

    def test_bias_falls_back_to_neutral_on_missing_inputs(self) -> None:
        self.assertEqual(
            policy_rate_bias(sofr_effr_spread_bps=None, repo_dispersion_bps=None, band_position=None), 0.0
        )

    def test_service_persists_and_builds_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "rates.db"
            service = PolicyRateIngestionService(store=PolicyRateStore(db_path), client=_client())
            result = service.ingest(start_date="2026-07-01", end_date="2026-07-31", rate_types=["EFFR", "SOFR"])
            self.assertEqual(result["observation_count"], 4)
            self.assertEqual(result["effective_date"], "2026-07-30")

            docs = PolicyRateStore(db_path).load_documents(per_type_limit=2)
            self.assertEqual(len(docs), 4)
            self.assertTrue(all(doc.kind == "policy_rate_observation" for doc in docs))

            history = build_policy_rate_bias_history(db_path=db_path)
            self.assertEqual([entry["date"] for entry in history], ["2026-07-29", "2026-07-30"])
            self.assertAlmostEqual(history[-1]["sofr_effr_spread_bps"], 2.0, places=4)

    def test_exports_dashboard_and_graph_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "rates.db"
            output_js = Path(temp_dir) / "policy_rate_dashboard_data.js"
            PolicyRateIngestionService(store=PolicyRateStore(db_path), client=_client()).ingest(
                start_date="2026-07-01", end_date="2026-07-31", rate_types=["EFFR", "SOFR"]
            )

            payload = build_dashboard_policy_rates_payload(db_path=db_path)
            self.assertIsNotNone(payload)
            assert payload is not None
            self.assertEqual(payload["metric_date"], "2026-07-30")
            metrics = payload["metrics"]
            self.assertEqual(metrics["effr"], 3.63)
            self.assertEqual(metrics["target_rate_mid"], 3.625)
            self.assertAlmostEqual(metrics["effr_band_position"], 0.52, places=2)
            self.assertAlmostEqual(metrics["sofr_effr_spread_bps"], 2.0, places=4)
            self.assertAlmostEqual(metrics["repo_dispersion_bps"], 13.0, places=4)
            self.assertEqual(payload["heat_card"]["label"], "Funding Stress")

            graph = build_policy_rate_knowledge_graph_payload(db_path=db_path)
            self.assertIsNotNone(graph)
            assert graph is not None
            self.assertGreater(graph["policy_rate_graph"]["stats"]["observation_count"], 0)

            exported = export_dashboard_policy_rates_js(db_path=db_path, output_js_path=output_js)
            self.assertIsNotNone(exported)
            text = output_js.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("window.__POLICY_RATE_DASHBOARD_DATA__ = "))
            parsed = json.loads(text.split("=", 1)[1].strip().rstrip(";"))
            self.assertIn("policy_rate_graph", parsed)
            self.assertIn("bias_history", parsed)

    def test_returns_none_without_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "empty.db"
            PolicyRateStore(db_path).initialize()
            self.assertIsNone(build_dashboard_policy_rates_payload(db_path=db_path))


if __name__ == "__main__":
    unittest.main()
