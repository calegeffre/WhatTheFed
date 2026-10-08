from datetime import date

from whatthefed.fed_history import (
    TargetObservation,
    build_history_payload,
    parse_current_statement_meetings,
    parse_historical_meeting_dates,
    parse_target_csv,
)


def test_parse_historical_meeting_dates_uses_final_day() -> None:
    page = """
    <h5>January 31-February 1 Meeting - 1982</h5>
    <h5>May 20 Conference Call - 1982</h5>
    <h5>Minutes release</h5>
    """

    assert parse_historical_meeting_dates(page) == [
        date(1982, 2, 1),
        date(1982, 5, 20),
    ]


def test_parse_current_statement_meetings_builds_official_urls() -> None:
    page = """
    <strong>Statement:</strong><br>
    <a href="/monetarypolicy/files/monetary20260916a1.pdf">PDF</a> |
    <a href="/newsevents/pressreleases/monetary20260916a.htm">HTML</a>
    <a href="/newsevents/pressreleases/monetary20250822a.htm">
      Statement on Longer-Run Goals and Monetary Policy Strategy
    </a>
    """

    assert parse_current_statement_meetings(page) == {
        date(2026, 9, 16): (
            "https://www.federalreserve.gov/newsevents/pressreleases/"
            "monetary20260916a.htm"
        )
    }


def test_parse_target_csv_supports_scalar_and_range_targets() -> None:
    payload = parse_target_csv(
        "observation_date,DFEDTAR,DFEDTARL,DFEDTARU\n"
        "2008-12-15,1.0,.,.\n"
        "2008-12-16,.,0.0,0.25\n"
    )

    assert payload == [
        TargetObservation(date(2008, 12, 15), 1.0, 1.0),
        TargetObservation(date(2008, 12, 16), 0.0, 0.25),
    ]


def test_build_history_uses_first_post_meeting_effective_target() -> None:
    meetings = {
        date(2026, 7, 29): "https://example.test/july",
        date(2026, 9, 16): "https://example.test/september",
    }
    targets = [
        TargetObservation(date(2026, 7, 28), 3.5, 3.75),
        TargetObservation(date(2026, 7, 30), 3.5, 3.75),
        TargetObservation(date(2026, 9, 15), 3.5, 3.75),
        TargetObservation(date(2026, 9, 17), 3.75, 4.0),
    ]

    payload = build_history_payload(meetings, targets, generated_at="2026-09-17T00:00:00Z")

    assert [meeting["decision"] for meeting in payload["meetings"]] == ["hold", "raise"]
    assert payload["meetings"][1]["target_lower"] == 3.75
    assert payload["meetings"][1]["target_upper"] == 4.0
    assert payload["meetings"][1]["change_bps"] == 25
