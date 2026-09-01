"""Fetch upcoming Federal Reserve speeches from the official monthly calendar."""

from __future__ import annotations

import argparse
import calendar
import html
import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


FED_BASE_URL = "https://www.federalreserve.gov"
SPEECH_SECTION_RE = re.compile(
    r"<h4[^>]*>\s*Speeches\s*</h4>(?P<section>.*?)(?=<div[^>]+cal-nojs__rowTitle|$)",
    re.IGNORECASE | re.DOTALL,
)
SPEECH_ROW_RE = re.compile(
    r'<div class="col-xs-2">\s*<p>(?P<time>.*?)</p>\s*</div>\s*'
    r'<div class="col-xs-7">(?P<details>.*?)</div>\s*'
    r'<div class="col-xs-3">\s*<p>(?P<day>\d{1,2})</p>',
    re.IGNORECASE | re.DOTALL,
)
TAG_RE = re.compile(r"<[^>]+>")
EASTERN = ZoneInfo("America/New_York")


class FedEventsIngestionError(RuntimeError):
    """Raised when the official calendar cannot be parsed."""


@dataclass(frozen=True)
class FedEvent:
    event_date: str
    time_et: str
    speaker: str
    title: str
    venue: str
    is_chair: bool
    source_url: str
    watch_url: str | None = None


def _text(fragment: str) -> str:
    return " ".join(html.unescape(TAG_RE.sub(" ", fragment)).split())


def _detail_paragraphs(fragment: str) -> list[str]:
    return [
        text
        for paragraph in re.findall(r"<p(?:\s[^>]*)?>(.*?)</p>", fragment, re.IGNORECASE | re.DOTALL)
        if (text := _text(paragraph))
    ]


def _safe_watch_url(value: str | None) -> str | None:
    if not value:
        return None
    decoded = html.unescape(value)
    return decoded if urlparse(decoded).scheme == "https" else None


def _time_sort_key(value: str) -> int:
    normalized = value.replace(".", "").upper().strip()
    try:
        parsed = datetime.strptime(normalized, "%I:%M %p")
    except ValueError:
        return 24 * 60
    return parsed.hour * 60 + parsed.minute


def _event_is_upcoming(event: FedEvent, now: datetime) -> bool:
    event_day = date.fromisoformat(event.event_date)
    eastern_now = now.astimezone(EASTERN)
    if event_day != eastern_now.date():
        return event_day > eastern_now.date()
    minutes = _time_sort_key(event.time_et)
    if minutes == 24 * 60:
        return True
    event_time = datetime(
        event_day.year,
        event_day.month,
        event_day.day,
        minutes // 60,
        minutes % 60,
        tzinfo=EASTERN,
    )
    return event_time >= eastern_now


def parse_month_speeches(page_html: str, *, year: int, month: int, source_url: str) -> list[FedEvent]:
    section_match = SPEECH_SECTION_RE.search(page_html)
    if section_match is None:
        return []

    events: list[FedEvent] = []
    for match in SPEECH_ROW_RE.finditer(section_match.group("section")):
        details = match.group("details")
        paragraphs = _detail_paragraphs(details)
        if not paragraphs or not paragraphs[0].lower().startswith(("speech -", "testimony -")):
            continue

        heading = paragraphs[0]
        speaker = heading.split("-", 1)[1].strip() if "-" in heading else heading
        title_match = re.search(
            r'class=["\'][^"\']*calendar__title[^"\']*["\'][^>]*>\s*<em>(.*?)</em>',
            details,
            re.IGNORECASE | re.DOTALL,
        )
        title = _text(title_match.group(1)) if title_match else "Topic not yet published"
        venue = next(
            (
                paragraph
                for paragraph in paragraphs[1:]
                if paragraph not in {"Watch Live", title}
            ),
            "",
        )
        watch_match = re.search(
            r'<a[^>]+href=["\'](?P<href>[^"\']+)["\'][^>]+title=["\']Watch Live["\']',
            details,
            re.IGNORECASE,
        )
        event_date = date(year, month, int(match.group("day"))).isoformat()
        is_chair = bool(re.search(r"(?<!Vice )\bChair\b", speaker, re.IGNORECASE))
        events.append(
            FedEvent(
                event_date=event_date,
                time_et=_text(match.group("time")),
                speaker=speaker,
                title=title,
                venue=venue,
                is_chair=is_chair,
                source_url=source_url,
                watch_url=_safe_watch_url(watch_match.group("href") if watch_match else None),
            )
        )
    return events


def _month_sequence(start: date, count: int) -> list[tuple[int, int]]:
    months = []
    year, month = start.year, start.month
    for _ in range(count):
        months.append((year, month))
        month += 1
        if month == 13:
            year += 1
            month = 1
    return months


def _fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": "WhatTheFed/1.0"})
    with urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def build_events_payload(
    *,
    start_date: date,
    months: int = 4,
    fetch_text=_fetch_text,
    now: datetime | None = None,
) -> dict[str, object]:
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        raise FedEventsIngestionError("now must include a timezone.")
    events: list[FedEvent] = []
    calendar_urls: list[str] = []
    for index, (year, month) in enumerate(_month_sequence(start_date, months)):
        month_name = calendar.month_name[month].lower()
        source_url = f"{FED_BASE_URL}/newsevents/{year}-{month_name}.htm"
        calendar_urls.append(source_url)
        try:
            page_html = fetch_text(source_url)
        except HTTPError as error:
            if index > 0 and error.code == 404:
                continue
            raise
        events.extend(
            parse_month_speeches(
                page_html,
                year=year,
                month=month,
                source_url=source_url,
            )
        )

    upcoming = sorted(
        (
            event
            for event in events
            if event.event_date >= start_date.isoformat() and _event_is_upcoming(event, current_time)
        ),
        key=lambda event: (event.event_date, _time_sort_key(event.time_et), event.speaker),
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "as_of": start_date.isoformat(),
        "calendar_urls": calendar_urls,
        "events": [asdict(event) for event in upcoming],
    }


def export_events_js(payload: dict[str, object], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        "window.__FED_EVENTS_DASHBOARD_DATA__ = "
        + json.dumps(payload, sort_keys=True, indent=2)
        + ";\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--months", type=int, default=4)
    parser.add_argument("--dashboard-js", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.months < 1:
        raise FedEventsIngestionError("--months must be at least 1.")

    payload = build_events_payload(start_date=args.start_date, months=args.months)
    export_events_js(payload, args.dashboard_js)
    print(f"Exported {len(payload['events'])} upcoming Fed speaking events.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
