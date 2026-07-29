import json
import tempfile
import unittest
from pathlib import Path

from whatthefed.fomc_ingestion import (
    FOMC_BASE_URL,
    FOMCStatementStore,
    export_dashboard_fomc_js,
    export_dashboard_fomc_history_js,
    parse_calendar_statement_urls,
    parse_statement_html,
)


SAMPLE_CALENDAR_HTML = """
<html>
  <body>
    <a href="/newsevents/pressreleases/monetary20260318a.htm">March</a>
    <a href="/newsevents/pressreleases/monetary20260617a.htm">June</a>
    <a href="/newsevents/pressreleases/monetary20260429a.htm">April</a>
    <a href="/newsevents/pressreleases/monetary20260617a.htm">June duplicate</a>
  </body>
</html>
""".strip()

SAMPLE_STATEMENT_HTML = """
<div id="article">
  <div class="heading col-xs-12 col-sm-8 col-md-8">
    <p class="article__time">June 17, 2026</p>
    <h3 class="title">Federal Reserve issues FOMC statement</h3>
    <p class="releaseTime">For release at 2:00 p.m. EDT</p>
  </div>
  <div class="col-xs-12 col-sm-8 col-md-8">
    <p>The Federal Open Market Committee approved the following statement for release by a 12 - 0 vote:</p>
    <p>The Committee decided to maintain the target range for the federal funds rate at 3-1/2 to 3-3/4 percent. Inflation remains elevated and job gains are resilient.</p>
    <p>Economic activity and productivity growth remain strong as demand stays firm.</p>
    <p>For media inquiries, please email test@example.com.</p>
    <p><a href="/newsevents/pressreleases/monetary20260617a1.htm">Implementation Note issued June 17, 2026</a></p>
  </div>
</div>
<div id="lastUpdate">Last Update: June 17, 2026</div>
""".strip()

SAMPLE_DISSENT_HTML = """
<div id="article">
  <div class="heading col-xs-12 col-sm-8 col-md-8">
    <p class="article__time">April 29, 2026</p>
    <h3 class="title">Federal Reserve issues FOMC statement</h3>
  </div>
  <div class="col-xs-12 col-sm-8 col-md-8">
    <p>The Federal Open Market Committee approved the following statement for release by a vote of 8-4.</p>
    <p>The Committee decided to maintain the target range for the federal funds rate at 3-1/2 to 3-3/4 percent.</p>
    <p>Governor Example preferred to reduce the target range by 25 basis points.</p>
  </div>
</div>
<div id="lastUpdate">Last Update: April 29, 2026</div>
""".strip()


class FOMCIngestionTests(unittest.TestCase):
    def test_parse_calendar_statement_urls_orders_latest_first(self) -> None:
        urls = parse_calendar_statement_urls(SAMPLE_CALENDAR_HTML, max_meetings=3)
        self.assertEqual(
            urls,
            [
                f"{FOMC_BASE_URL}/newsevents/pressreleases/monetary20260617a.htm",
                f"{FOMC_BASE_URL}/newsevents/pressreleases/monetary20260429a.htm",
                f"{FOMC_BASE_URL}/newsevents/pressreleases/monetary20260318a.htm",
            ],
        )

    def test_parse_statement_html_extracts_real_fields(self) -> None:
        statement = parse_statement_html(
            f"{FOMC_BASE_URL}/newsevents/pressreleases/monetary20260617a.htm",
            SAMPLE_STATEMENT_HTML,
        )
        self.assertEqual(statement.meeting_date, "2026-06-17")
        self.assertEqual(statement.decision, "hold")
        self.assertEqual(statement.vote_tally, "12-0")
        self.assertIn("maintain the target range", statement.content.lower())
        self.assertGreater(statement.inflation_mentions, 0)
        self.assertGreater(statement.labor_mentions, 0)
        self.assertGreater(statement.growth_mentions, 0)
        self.assertGreater(statement.policy_mentions, 0)

    def test_store_round_trip_loads_meeting_documents(self) -> None:
        statement = parse_statement_html(
            f"{FOMC_BASE_URL}/newsevents/pressreleases/monetary20260617a.htm",
            SAMPLE_STATEMENT_HTML,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            store = FOMCStatementStore(Path(temp_dir) / "fomc.db")
            store.write_statement(statement)

            documents = store.load_documents(limit=5)
            self.assertEqual(len(documents), 1)
            self.assertEqual(documents[0].kind, "meeting_note")
            self.assertEqual(documents[0].meeting_date, "2026-06-17")
            self.assertEqual(documents[0].metadata.get("vote_tally"), "12-0")

    def test_parse_statement_html_prefers_committee_decision_when_dissent_mentions_cut(self) -> None:
        statement = parse_statement_html(
            f"{FOMC_BASE_URL}/newsevents/pressreleases/monetary20260429a.htm",
            SAMPLE_DISSENT_HTML,
        )
        self.assertEqual(statement.decision, "hold")
        self.assertEqual(statement.vote_tally, "8-4")

    def test_exports_dashboard_signal_js_payload(self) -> None:
        statement = parse_statement_html(
            f"{FOMC_BASE_URL}/newsevents/pressreleases/monetary20260617a.htm",
            SAMPLE_STATEMENT_HTML,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "fomc.db"
            js_path = Path(temp_dir) / "fomc_dashboard_data.js"
            store = FOMCStatementStore(db_path)
            store.write_statement(statement)

            payload = export_dashboard_fomc_js(db_path=db_path, output_js_path=js_path)
            self.assertIsNotNone(payload)
            self.assertEqual(payload["meeting_date"], "2026-06-17")
            self.assertEqual(len(payload["signals"]), 4)
            self.assertEqual(payload["signals"][0]["label"], "Inflation Pressure")

            js_text = js_path.read_text(encoding="utf-8")
            self.assertTrue(js_text.startswith("window.__FOMC_DASHBOARD_DATA__ = "))
            parsed_payload = json.loads(js_text.split("=", 1)[1].strip().rstrip(";"))
            self.assertEqual(parsed_payload["meeting_date"], "2026-06-17")

    def test_exports_history_js_grouped_by_year(self) -> None:
        june_stmt = parse_statement_html(
            f"{FOMC_BASE_URL}/newsevents/pressreleases/monetary20260617a.htm",
            SAMPLE_STATEMENT_HTML,
        )
        march_stmt = parse_statement_html(
            f"{FOMC_BASE_URL}/newsevents/pressreleases/monetary20260318a.htm",
            SAMPLE_STATEMENT_HTML.replace("June 17, 2026", "March 18, 2026"),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "fomc.db"
            js_path = Path(temp_dir) / "fomc_history_data.js"
            store = FOMCStatementStore(db_path)
            store.write_statement(june_stmt)
            store.write_statement(march_stmt)

            payload = export_dashboard_fomc_history_js(db_path=db_path, output_js_path=js_path)
            self.assertIn("2026", payload)
            self.assertEqual(len(payload["2026"]), 2)
            self.assertEqual(payload["2026"][0]["meeting_date"], "2026-06-17")

            js_text = js_path.read_text(encoding="utf-8")
            self.assertTrue(js_text.startswith("window.__FOMC_HISTORY_DATA__ = "))


if __name__ == "__main__":
    unittest.main()
