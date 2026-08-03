import json
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

from whatthefed.gdp_ingestion import (
    BEAQuarterlyGDPClient,
    GDPIngestionService,
    GDPStore,
    build_dashboard_gdp_payload,
    build_gdp_bias_history,
    build_gdp_knowledge_graph_payload,
    export_dashboard_gdp_js,
    gdp_bias,
)


def _workbook() -> bytes:
    shared = [
        "Line",
        "2024Q1",
        "2024Q2",
        "2024Q3",
        "2024Q4",
        "2025Q1",
        "2025Q2",
        "Gross domestic product",
        "A191RL",
        "Personal consumption expenditures",
        "DPCERL",
        "Gross private domestic investment",
        "A006RL",
        "Fixed investment",
        "A007RL",
        "Exports",
        "A020RL",
        "Imports",
        "A021RL",
        "Government",
        "A822RL",
    ]
    shared_xml = "".join(f"<si><t>{value}</t></si>" for value in shared)
    header = "".join(
        f'<c r="{column}8" t="s"><v>{index}</v></c>'
        for column, index in zip(("A", "D", "E", "F", "G", "H", "I"), range(7))
    )
    codes = [(7, 8), (9, 10), (11, 12), (13, 14), (15, 16), (17, 18), (19, 20)]
    rows = []
    for row_number, (label_index, code_index) in enumerate(codes, start=9):
        values = [1.0, 2.5, 3.0, 1.5, 2.0, 3.5]
        cells = (
            f'<c r="A{row_number}"><v>{row_number - 8}</v></c>'
            f'<c r="B{row_number}" t="s"><v>{label_index}</v></c>'
            f'<c r="C{row_number}" t="s"><v>{code_index}</v></c>'
            + "".join(
                f'<c r="{column}{row_number}"><v>{value + (row_number - 9) / 10}</v></c>'
                for column, value in zip(("D", "E", "F", "G", "H", "I"), values)
            )
        )
        rows.append(f'<row r="{row_number}">{cells}</row>')
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData><row r="8">{header}</row>{"".join(rows)}</sheetData></worksheet>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="T10101-Q" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Target="worksheets/sheet1.xml" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>'
        '</Relationships>'
    )
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr(
            "xl/sharedStrings.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"{shared_xml}</sst>",
        )
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return output.getvalue()


class GDPTests(unittest.TestCase):
    def test_client_reads_quarterly_workbook(self) -> None:
        client = BEAQuarterlyGDPClient(get_bytes=lambda _url: _workbook())
        observations, _ = client.fetch_observations(
            series_ids=["A191RL", "DPCERL"], start_year=2025
        )
        self.assertEqual(len(observations), 4)
        self.assertEqual(observations[-1].period, "2025Q2")

    def test_bias_maps_above_trend_growth_hawkish(self) -> None:
        self.assertEqual(gdp_bias(2.0), 0)
        self.assertGreater(gdp_bias(3.5), 0)
        self.assertLess(gdp_bias(-1.0), 0)

    def test_ingest_dashboard_graph_and_export(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "gdp.db"
            service = GDPIngestionService(
                GDPStore(db_path), BEAQuarterlyGDPClient(get_bytes=lambda _url: _workbook())
            )
            result = service.ingest(start_year=2024)
            self.assertEqual(result["observation_count"], 42)
            history = build_gdp_bias_history(db_path=db_path)
            self.assertEqual(len(history), 6)
            payload = build_dashboard_gdp_payload(db_path=db_path)
            self.assertIsNotNone(payload)
            assert payload is not None
            self.assertEqual(payload["heat_card"]["label"], "Real GDP Growth")
            graph = build_gdp_knowledge_graph_payload(db_path=db_path)
            self.assertEqual(graph["gdp_graph"]["stats"]["series_count"], 7)

            output = Path(temp) / "gdp.js"
            export_dashboard_gdp_js(db_path=db_path, output_js_path=output)
            text = output.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("window.__GDP_DASHBOARD_DATA__ = "))
            self.assertIn("gdp_graph", json.loads(text.split("=", 1)[1].strip().rstrip(";")))

    def test_invalid_workbook_is_rejected(self) -> None:
        client = BEAQuarterlyGDPClient(get_bytes=lambda _url: b"not an xlsx")
        with self.assertRaises(RuntimeError):
            client.fetch_observations(series_ids=["A191RL"])


if __name__ == "__main__":
    unittest.main()
