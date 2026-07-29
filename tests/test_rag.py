import unittest

from whatthefed.rag import Document, FedRAGAnalyzer


class FedRAGAnalyzerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.analyzer = FedRAGAnalyzer(top_k=4)
        self.meeting_notes = [
            Document(
                source="fomc_2026_03",
                content="The committee held rates steady as inflation moderated modestly.",
                kind="meeting_note",
                meeting_date="2026-03-19",
            ),
            Document(
                source="fomc_2026_06",
                content=(
                    "The committee held rates unchanged. "
                    "Members emphasized persistent inflation and resilient labor conditions."
                ),
                kind="meeting_note",
                meeting_date="2026-06-17",
            ),
        ]

        self.trusted_signals = [
            Document(source="cpi", content="Inflation remains elevated in core services.", kind="trusted_signal"),
            Document(source="jobs", content="Labor market is resilient with strong hiring.", kind="trusted_signal"),
            Document(source="gdp", content="Growth is slowing and demand is softening.", kind="trusted_signal"),
        ]

    def test_summarizes_latest_meeting_note(self) -> None:
        summary = self.analyzer.summarize_last_meeting(self.meeting_notes)
        self.assertIn("held rates unchanged", summary.lower())
        self.assertIn("persistent inflation", summary.lower())

    def test_retrieval_returns_relevant_documents(self) -> None:
        docs = self.analyzer.retrieve(
            "inflation resilient labor policy rates", [*self.meeting_notes, *self.trusted_signals]
        )
        self.assertGreaterEqual(len(docs), 1)
        self.assertEqual(docs[0].source, "fomc_2026_06")

    def test_prediction_includes_decision_confidence_and_rationale(self) -> None:
        report = self.analyzer.analyze(self.meeting_notes, self.trusted_signals)
        prediction = report["next_meeting_prediction"]
        self.assertIn(prediction["decision"], {"raise", "hold", "cut"})
        self.assertGreaterEqual(prediction["confidence"], 0.33)
        self.assertIn("hawkish", prediction["rationale"])

    def test_analyze_returns_dashboard_ready_data(self) -> None:
        report = self.analyzer.analyze(self.meeting_notes, self.trusted_signals)
        self.assertEqual(report["last_meeting_label"], "June 2026 Meeting")
        self.assertEqual(report["last_meeting_decision"], "hold")
        self.assertEqual(len(report["dashboard"]["last_meeting_votes"]), 12)
        self.assertEqual(len(report["dashboard"]["next_meeting_heat_map"]), 4)
        self.assertEqual(report["dashboard"]["last_meeting_votes"][0]["member"], "Member 01")

    def test_analyze_uses_unanimous_vote_tally_when_present(self) -> None:
        report = self.analyzer.analyze(
            meeting_notes=[
                Document(
                    source="fomc_2026_06",
                    content=(
                        "The Federal Open Market Committee approved the following statement "
                        "for release by a 12-0 vote. "
                        "The committee held rates unchanged. "
                        "Members emphasized persistent inflation and resilient labor conditions."
                    ),
                    kind="meeting_note",
                    meeting_date="2026-06-17",
                )
            ],
            trusted_signals=self.trusted_signals,
        )
        self.assertEqual(len(report["dashboard"]["last_meeting_votes"]), 12)
        self.assertTrue(all(vote["vote"] == "hold" for vote in report["dashboard"]["last_meeting_votes"]))


if __name__ == "__main__":
    unittest.main()
