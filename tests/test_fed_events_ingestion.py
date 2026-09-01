from datetime import date, datetime, timezone
from io import BytesIO
from urllib.error import HTTPError

from whatthefed.fed_events_ingestion import build_events_payload, parse_month_speeches


SAMPLE_MONTH_HTML = """
<div class="row cal-nojs__rowTitle"><h4 class="col-md-12">Speeches </h4></div>
<div class="row">
  <div class="panel panel-unstyled"><div class="panel-body"><div class="row">
    <div class="col-xs-2"><p>10:00 a.m.</p></div>
    <div class="col-xs-7">
      <p>Speech - Chair Kevin Warsh</p>
      <p><a class="watchLive" href="https://example.com/live" title="Watch Live">Watch Live</a></p>
      <p class="calendar__title"><em>Economic Outlook</em></p>
      <p>At the Policy Forum, New York, New York</p>
    </div>
    <div class="col-xs-3"><p>12</p></div>
  </div></div></div>
</div>
<div class="row">
  <div class="panel panel-unstyled"><div class="panel-body"><div class="row">
    <div class="col-xs-2"><p>2:30 p.m.</p></div>
    <div class="col-xs-7">
      <p>Speech - Vice Chair Example Person</p>
      <p class="calendar__title"><em>Financial Stability</em></p>
      <p>At a virtual conference</p>
    </div>
    <div class="col-xs-3"><p>4</p></div>
  </div></div></div>
</div>
<div class="row cal-nojs__rowTitle"><h4 class="col-md-12">FOMC Meetings</h4></div>
"""


def test_parse_month_speeches_extracts_chair_and_watch_link() -> None:
    events = parse_month_speeches(
        SAMPLE_MONTH_HTML,
        year=2026,
        month=9,
        source_url="https://www.federalreserve.gov/newsevents/2026-september.htm",
    )

    assert len(events) == 2
    assert events[0].event_date == "2026-09-12"
    assert events[0].speaker == "Chair Kevin Warsh"
    assert events[0].title == "Economic Outlook"
    assert events[0].venue == "At the Policy Forum, New York, New York"
    assert events[0].watch_url == "https://example.com/live"
    assert events[0].is_chair is True
    assert events[1].is_chair is False


def test_build_events_payload_filters_past_events() -> None:
    payload = build_events_payload(
        start_date=date(2026, 9, 8),
        months=1,
        fetch_text=lambda _: SAMPLE_MONTH_HTML,
        now=datetime(2026, 9, 8, tzinfo=timezone.utc),
    )

    assert len(payload["events"]) == 1
    assert payload["events"][0]["event_date"] == "2026-09-12"


def test_build_events_payload_filters_completed_same_day_event() -> None:
    payload = build_events_payload(
        start_date=date(2026, 9, 12),
        months=1,
        fetch_text=lambda _: SAMPLE_MONTH_HTML,
        now=datetime(2026, 9, 12, 15, 0, tzinfo=timezone.utc),
    )

    assert payload["events"] == []


def test_build_events_payload_skips_unpublished_future_month() -> None:
    calls = 0

    def fetch_text(url: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return SAMPLE_MONTH_HTML
        raise HTTPError(url, 404, "Not Found", {}, BytesIO())

    payload = build_events_payload(
        start_date=date(2026, 9, 8),
        months=2,
        fetch_text=fetch_text,
        now=datetime(2026, 9, 8, tzinfo=timezone.utc),
    )

    assert len(payload["events"]) == 1
    assert len(payload["calendar_urls"]) == 2
