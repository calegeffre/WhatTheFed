"""Build comparable FOMC meeting and target-rate history from official sources."""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import re
import time
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen


FED_BASE_URL = "https://www.federalreserve.gov"
FOMC_CALENDAR_URL = f"{FED_BASE_URL}/monetarypolicy/fomccalendars.htm"
FOMC_HISTORICAL_URL = f"{FED_BASE_URL}/monetarypolicy/fomchistorical{{year}}.htm"
FRED_TARGET_URL = (
    "https://fred.stlouisfed.org/graph/fredgraph.csv"
    "?id=DFEDTAR,DFEDTARL,DFEDTARU&cosd=1982-01-01"
)
FETCH_RETRY_DELAYS_SECONDS = (5, 15)
H5_RE = re.compile(r"<h5[^>]*>(.*?)</h5>", re.IGNORECASE | re.DOTALL)
STATEMENT_RE = re.compile(
    r"<strong>\s*Statement:\s*</strong>.{0,500}?"
    r'href=["\'](?P<href>/newsevents/pressreleases/monetary(?P<date>\d{8})a\.htm)["\']',
    re.IGNORECASE | re.DOTALL,
)
MONTHS = {
    month: index
    for index, month in enumerate(
        (
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ),
        start=1,
    )
}
MEETING_HEADING_RE = re.compile(
    r"^(?P<start_month>[A-Za-z]+)\s+(?P<start_day>\d{1,2})"
    r"(?:-(?:(?P<end_month>[A-Za-z]+)\s+)?(?P<end_day>\d{1,2}))?"
    r"\s+(?:Meeting|Conference Call)\s+-\s+(?P<year>\d{4})$",
    re.IGNORECASE,
)


class FedHistoryError(RuntimeError):
    """Raised when official meeting or target-rate history cannot be built."""


@dataclass(frozen=True)
class TargetObservation:
    observation_date: date
    lower: float
    upper: float

    @property
    def midpoint(self) -> float:
        return (self.lower + self.upper) / 2


def parse_historical_meeting_dates(page_html: str) -> list[date]:
    meetings: list[date] = []
    for raw_heading in H5_RE.findall(page_html):
        heading = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw_heading))).strip()
        match = MEETING_HEADING_RE.match(heading)
        if match is None:
            continue
        end_month = match.group("end_month") or match.group("start_month")
        end_day = match.group("end_day") or match.group("start_day")
        try:
            meetings.append(
                date(
                    int(match.group("year")),
                    MONTHS[end_month.lower()],
                    int(end_day),
                )
            )
        except (KeyError, ValueError):
            continue
    return meetings


def parse_current_statement_meetings(calendar_html: str) -> dict[date, str]:
    return {
        datetime.strptime(match.group("date"), "%Y%m%d").date(): FED_BASE_URL + match.group("href")
        for match in STATEMENT_RE.finditer(calendar_html)
    }


def parse_target_csv(csv_text: str) -> list[TargetObservation]:
    observations: list[TargetObservation] = []
    for row in csv.DictReader(io.StringIO(csv_text)):
        raw_date = str(row.get("observation_date") or "")
        scalar = _float_or_none(row.get("DFEDTAR"))
        lower = _float_or_none(row.get("DFEDTARL"))
        upper = _float_or_none(row.get("DFEDTARU"))
        if scalar is not None:
            lower = upper = scalar
        if lower is None or upper is None:
            continue
        try:
            observation_date = date.fromisoformat(raw_date)
        except ValueError:
            continue
        observations.append(TargetObservation(observation_date, lower, upper))
    observations.sort(key=lambda item: item.observation_date)
    if not observations:
        raise FedHistoryError("FRED target-rate history returned no usable observations.")
    return observations


def build_history_payload(
    meeting_sources: dict[date, str],
    targets: list[TargetObservation],
    *,
    generated_at: str | None = None,
) -> dict[str, object]:
    target_dates = [item.observation_date for item in targets]
    meetings: list[dict[str, object]] = []
    previous_target: TargetObservation | None = None
    for meeting_date in sorted(meeting_sources):
        post_index = bisect_right(target_dates, meeting_date)
        if post_index >= len(targets):
            continue
        post = targets[post_index]
        if post.observation_date > meeting_date + timedelta(days=7):
            continue
        delta = post.midpoint - previous_target.midpoint if previous_target is not None else 0.0
        decision = "raise" if delta > 0.001 else "cut" if delta < -0.001 else "hold"
        meetings.append(
            {
                "meeting_date": meeting_date.isoformat(),
                "label": meeting_date.strftime("%B %Y"),
                "decision": decision,
                "target_lower": round(post.lower, 4),
                "target_upper": round(post.upper, 4),
                "change_bps": round(delta * 100),
                "source_url": meeting_sources[meeting_date],
            }
        )
        previous_target = post

    if not meetings:
        raise FedHistoryError("No FOMC meetings could be aligned to target-rate history.")
    return {
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "coverage_start": meetings[0]["meeting_date"],
        "coverage_end": meetings[-1]["meeting_date"],
        "methodology": (
            "Official FOMC meeting and conference-call dates aligned to the first FRED "
            "target-rate observation after each event. Changes compare each aligned target "
            "with the prior recorded event, which keeps pre-1994 intermeeting target moves "
            "visible; comparable target-rate history begins in 1982."
        ),
        "meetings": meetings,
    }


def fetch_history_payload(*, start_year: int = 1982) -> dict[str, object]:
    current_html = _fetch_text(FOMC_CALENDAR_URL)
    current_sources = parse_current_statement_meetings(current_html)
    latest_current_year = max((item.year for item in current_sources), default=date.today().year)
    meeting_sources: dict[date, str] = {}
    for year in range(start_year, min(2020, latest_current_year) + 1):
        url = FOMC_HISTORICAL_URL.format(year=year)
        page_html = _fetch_text(url)
        for meeting_date in parse_historical_meeting_dates(page_html):
            meeting_sources[meeting_date] = url
    meeting_sources.update(current_sources)
    return build_history_payload(meeting_sources, parse_target_csv(_fetch_text(FRED_TARGET_URL)))


def export_history_js(payload: dict[str, object], output_path: str | Path) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "window.__FED_RATE_HISTORY_DATA__ = "
        + json.dumps(payload, sort_keys=True, indent=2)
        + ";\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build official FOMC target-rate history.")
    parser.add_argument("--start-year", type=int, default=1982)
    parser.add_argument("--output-js", required=True)
    args = parser.parse_args(argv)
    payload = fetch_history_payload(start_year=args.start_year)
    export_history_js(payload, args.output_js)
    print(f"Exported {len(payload['meetings'])} FOMC meetings to {args.output_js}.")
    return 0


def _fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": "WhatTheFed/1.0"})
    for attempt in range(len(FETCH_RETRY_DELAYS_SECONDS) + 1):
        try:
            with urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8", errors="replace")
        except TimeoutError:
            if attempt == len(FETCH_RETRY_DELAYS_SECONDS):
                raise
            time.sleep(FETCH_RETRY_DELAYS_SECONDS[attempt])
    raise AssertionError("Unreachable")


def _float_or_none(value: object) -> float | None:
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return None
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
