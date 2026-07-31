from __future__ import annotations

import argparse
import contextlib
import html
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from .rag import Document


DEFAULT_DB_PATH = Path("data") / "market_snapshots.db"
FOMC_BASE_URL = "https://www.federalreserve.gov"
FOMC_CALENDAR_URL = f"{FOMC_BASE_URL}/monetarypolicy/fomccalendars.htm"

CALENDAR_STATEMENT_LINK_RE = re.compile(
    r'href=["\'](?P<href>(?:https?://www\.federalreserve\.gov)?/newsevents/pressreleases/monetary(?P<date>\d{8})a\.htm)["\']',
    re.IGNORECASE,
)
ARTICLE_BLOCK_RE = re.compile(r'<div id="article">(.*?)<div id="lastUpdate"', re.IGNORECASE | re.DOTALL)
EIGHT_COL_RE = re.compile(
    r'<div class="col-xs-12 col-sm-8 col-md-8">(.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)
PARAGRAPH_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)
TITLE_RE = re.compile(r'<h3 class="title">\s*(.*?)\s*</h3>', re.IGNORECASE | re.DOTALL)
ARTICLE_TIME_RE = re.compile(r'<p class="article__time">\s*(.*?)\s*</p>', re.IGNORECASE | re.DOTALL)
RELEASE_TIME_RE = re.compile(r'<p class="releaseTime">\s*(.*?)\s*(?:</p>|<ul)', re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")
VOTE_TALLY_PATTERNS = (
    re.compile(r"\b(?P<for_votes>\d+)\s*[–-]\s*(?P<against_votes>\d+)\s+vote\b", re.IGNORECASE),
    re.compile(r"\bvote\s+of\s+(?P<for_votes>\d+)\s*[–-]\s*(?P<against_votes>\d+)\b", re.IGNORECASE),
)
# Matches the "Voting for the monetary policy action were ..." sentence in older statements.
# Capture group 1 is everything up to the start of "Voting against" or the closing </p>.
VOTING_FOR_RE = re.compile(
    r"Voting for the monetary policy action were\s+([^<]+?)(?=Voting against|</p>)",
    re.IGNORECASE | re.DOTALL,
)
FOMC_COMMITTEE_SIZE = 12

INFLATION_TERMS = (
    "inflation",
    "price stability",
    "price increases",
    "2 percent goal",
)
LABOR_TERMS = (
    "labor market",
    "job gains",
    "unemployment",
    "workforce",
    "hiring",
)
GROWTH_TERMS = (
    "economic activity",
    "growth",
    "productivity",
    "investment",
    "demand",
)
POLICY_TERMS = (
    "target range",
    "federal funds rate",
    "committee decided",
    "dual mandate",
)
HAWKISH_TERMS = ("inflation", "overheat", "tight", "resilient", "strong labor")
DOVISH_TERMS = ("disinflation", "slowdown", "recession", "weak", "softening")
FOMC_SIGNAL_LABELS = (
    ("inflation_mentions", "Inflation Pressure"),
    ("labor_mentions", "Labor Strength"),
    ("growth_mentions", "Growth Momentum"),
)

_DISSENT_RAISE_RE = re.compile(r"preferred\s+to\s+(?:raise|increase|tighten)", re.I)
_DISSENT_CUT_RE = re.compile(r"preferred\s+to\s+(?:reduce|lower|decrease|ease|cut)", re.I)

TextFetcher = Callable[[str], str]


class FOMCIngestionError(RuntimeError):
    pass


@dataclass(frozen=True)
class FOMCStatement:
    meeting_date: str
    statement_url: str
    title: str
    release_time: str | None
    vote_tally: str | None
    decision: str
    summary: str
    content: str
    inflation_mentions: int
    labor_mentions: int
    growth_mentions: int
    policy_mentions: int
    hawkish_mentions: int
    dovish_mentions: int
    fetched_at: str

    @property
    def meeting_label(self) -> str:
        try:
            return datetime.fromisoformat(self.meeting_date).strftime("%B %Y")
        except ValueError:
            return self.meeting_date


class FOMCStatementStore:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._open_connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS fomc_statements (
                    meeting_date TEXT NOT NULL,
                    statement_url TEXT NOT NULL PRIMARY KEY,
                    title TEXT NOT NULL,
                    release_time TEXT,
                    vote_tally TEXT,
                    decision TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    content TEXT NOT NULL,
                    inflation_mentions INTEGER NOT NULL,
                    labor_mentions INTEGER NOT NULL,
                    growth_mentions INTEGER NOT NULL,
                    policy_mentions INTEGER NOT NULL,
                    hawkish_mentions INTEGER NOT NULL,
                    dovish_mentions INTEGER NOT NULL,
                    fetched_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_fomc_statements_meeting_date
                ON fomc_statements (meeting_date DESC);
                """
            )

    def write_statement(self, statement: FOMCStatement) -> None:
        self.initialize()
        metadata = {
            "meeting_label": statement.meeting_label,
            "vote_tally": statement.vote_tally,
            "signal_counts": {
                "inflation": statement.inflation_mentions,
                "labor": statement.labor_mentions,
                "growth": statement.growth_mentions,
                "policy": statement.policy_mentions,
            },
            "tone_counts": {
                "hawkish": statement.hawkish_mentions,
                "dovish": statement.dovish_mentions,
            },
        }
        with self._open_connection() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO fomc_statements (
                    meeting_date, statement_url, title, release_time, vote_tally, decision,
                    summary, content, inflation_mentions, labor_mentions, growth_mentions,
                    policy_mentions, hawkish_mentions, dovish_mentions, fetched_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    statement.meeting_date,
                    statement.statement_url,
                    statement.title,
                    statement.release_time,
                    statement.vote_tally,
                    statement.decision,
                    statement.summary,
                    statement.content,
                    statement.inflation_mentions,
                    statement.labor_mentions,
                    statement.growth_mentions,
                    statement.policy_mentions,
                    statement.hawkish_mentions,
                    statement.dovish_mentions,
                    statement.fetched_at,
                    json.dumps(metadata, sort_keys=True),
                ),
            )

    def load_documents(self, *, limit: int = 24) -> list[Document]:
        self.initialize()
        with self._open_connection() as connection:
            rows = connection.execute(
                """
                SELECT meeting_date, statement_url, title, release_time, vote_tally, decision,
                       summary, content, inflation_mentions, labor_mentions, growth_mentions,
                       policy_mentions, hawkish_mentions, dovish_mentions, fetched_at, metadata_json
                FROM fomc_statements
                ORDER BY meeting_date DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        documents: list[Document] = []
        for row in rows:
            metadata = json.loads(row["metadata_json"] or "{}")
            documents.append(
                Document(
                    source=f"fomc_{str(row['meeting_date']).replace('-', '_')}",
                    content=row["content"],
                    kind="meeting_note",
                    meeting_date=row["meeting_date"],
                    published_at=row["fetched_at"],
                    source_url=row["statement_url"],
                    metadata={
                        "title": row["title"],
                        "release_time": row["release_time"],
                        "decision": row["decision"],
                        "vote_tally": row["vote_tally"],
                        "summary": row["summary"],
                        "inflation_mentions": row["inflation_mentions"],
                        "labor_mentions": row["labor_mentions"],
                        "growth_mentions": row["growth_mentions"],
                        "policy_mentions": row["policy_mentions"],
                        "hawkish_mentions": row["hawkish_mentions"],
                        "dovish_mentions": row["dovish_mentions"],
                        **metadata,
                    },
                )
            )
        return documents

    def statement_count(self) -> int:
        self.initialize()
        with self._open_connection() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM fomc_statements").fetchone()
        return int(row["count"]) if row is not None else 0

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    @contextlib.contextmanager
    def _open_connection(self) -> Iterable[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()


class FOMCIngestionService:
    def __init__(
        self,
        store: FOMCStatementStore,
        *,
        calendar_url: str = FOMC_CALENDAR_URL,
        fetch_text: TextFetcher | None = None,
    ) -> None:
        self.store = store
        self.calendar_url = calendar_url
        self.fetch_text = fetch_text or _fetch_text

    def ingest(self, *, max_meetings: int = 12) -> list[FOMCStatement]:
        calendar_html = self.fetch_text(self.calendar_url)
        statement_urls = parse_calendar_statement_urls(calendar_html, max_meetings=max_meetings)
        statements: list[FOMCStatement] = []
        for statement_url in statement_urls:
            statement_html = self.fetch_text(statement_url)
            statement = parse_statement_html(statement_url, statement_html)
            self.store.write_statement(statement)
            statements.append(statement)
        return statements


def parse_calendar_statement_urls(calendar_html: str, *, max_meetings: int | None = None) -> list[str]:
    matches = list(CALENDAR_STATEMENT_LINK_RE.finditer(calendar_html))
    if not matches:
        raise FOMCIngestionError("No FOMC statement links were found in the calendar page.")

    unique_by_date: dict[str, str] = {}
    for match in matches:
        raw_href = match.group("href")
        date_token = match.group("date")
        resolved = urljoin(FOMC_BASE_URL, raw_href)
        unique_by_date[date_token] = resolved

    ordered_dates = sorted(unique_by_date.keys(), reverse=True)
    if max_meetings is not None and max_meetings > 0:
        ordered_dates = ordered_dates[:max_meetings]
    return [unique_by_date[date_token] for date_token in ordered_dates]


def parse_statement_html(statement_url: str, statement_html: str) -> FOMCStatement:
    article_block_match = ARTICLE_BLOCK_RE.search(statement_html)
    article_block = article_block_match.group(1) if article_block_match else statement_html

    title = _extract_clean_text(article_block, TITLE_RE) or "Federal Reserve issues FOMC statement"
    article_time = _extract_clean_text(article_block, ARTICLE_TIME_RE)
    release_time = _extract_clean_text(article_block, RELEASE_TIME_RE)
    meeting_date = _coerce_meeting_date(article_time=article_time, statement_url=statement_url)
    paragraphs = _extract_statement_paragraphs(article_block)
    if not paragraphs:
        raise FOMCIngestionError(f"Unable to parse statement paragraphs from {statement_url}.")

    content = "\n\n".join(paragraphs)
    summary = " ".join(paragraphs[:2]).strip()
    vote_tally = _extract_vote_tally(article_block)
    decision = _infer_decision(content)
    lowered = content.lower()

    return FOMCStatement(
        meeting_date=meeting_date,
        statement_url=statement_url,
        title=title,
        release_time=release_time,
        vote_tally=vote_tally,
        decision=decision,
        summary=summary,
        content=content,
        inflation_mentions=_count_terms(lowered, INFLATION_TERMS),
        labor_mentions=_count_terms(lowered, LABOR_TERMS),
        growth_mentions=_count_terms(lowered, GROWTH_TERMS),
        policy_mentions=_count_terms(lowered, POLICY_TERMS),
        hawkish_mentions=_count_terms(lowered, HAWKISH_TERMS),
        dovish_mentions=_count_terms(lowered, DOVISH_TERMS),
        fetched_at=_utcnow().isoformat(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One-off ingestion of official FOMC statements from the Fed calendar into SQLite."
    )
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="SQLite database path.")
    parser.add_argument(
        "--max-meetings",
        type=int,
        default=12,
        help="Number of most recent statement meetings to ingest from the calendar page.",
    )
    parser.add_argument(
        "--dashboard-js",
        help="Optional path to write a JS payload (window.__FOMC_DASHBOARD_DATA__) for index.html.",
    )
    parser.add_argument(
        "--dashboard-meeting-date",
        help="Optional meeting date (YYYY-MM-DD) to export into the dashboard payload. Defaults to latest meeting.",
    )
    parser.add_argument(
        "--history-js",
        help="Optional path to write per-year FOMC meeting history as JS (window.__FOMC_HISTORY_DATA__) for index.html.",
    )
    args = parser.parse_args(argv)

    store = FOMCStatementStore(args.db_path)
    service = FOMCIngestionService(store=store)
    statements = service.ingest(max_meetings=args.max_meetings)
    if args.dashboard_js:
        export_dashboard_fomc_js(
            db_path=args.db_path,
            output_js_path=args.dashboard_js,
            meeting_date=args.dashboard_meeting_date,
        )
    if args.history_js:
        export_dashboard_fomc_history_js(db_path=args.db_path, output_js_path=args.history_js)
    print(f"Ingested {len(statements)} FOMC statements into {args.db_path}.")
    return 0


def _extract_statement_paragraphs(article_block: str) -> list[str]:
    column_matches = EIGHT_COL_RE.findall(article_block)
    body_html = column_matches[1] if len(column_matches) > 1 else column_matches[-1] if column_matches else article_block
    raw_paragraphs = PARAGRAPH_RE.findall(body_html)
    cleaned_paragraphs = [_clean_fragment(paragraph) for paragraph in raw_paragraphs]
    return [
        paragraph
        for paragraph in cleaned_paragraphs
        if paragraph
        and not paragraph.lower().startswith("for media inquiries")
        and "implementation note issued" not in paragraph.lower()
    ]


def _extract_clean_text(content: str, pattern: re.Pattern[str]) -> str | None:
    match = pattern.search(content)
    if match is None:
        return None
    return _clean_fragment(match.group(1))


def _clean_fragment(value: str) -> str:
    unescaped = html.unescape(value)
    without_tags = TAG_RE.sub(" ", unescaped)
    return WHITESPACE_RE.sub(" ", without_tags).strip()


def _coerce_meeting_date(*, article_time: str | None, statement_url: str) -> str:
    if article_time:
        for date_format in ("%B %d, %Y", "%b %d, %Y"):
            try:
                return datetime.strptime(article_time, date_format).date().isoformat()
            except ValueError:
                continue
    date_match = re.search(r"monetary(\d{8})a\.htm", statement_url)
    if date_match is None:
        raise FOMCIngestionError(f"Unable to infer meeting date from statement URL: {statement_url}")
    return datetime.strptime(date_match.group(1), "%Y%m%d").date().isoformat()


def _extract_vote_tally(content: str) -> str | None:
    # Modern format: numeric tally embedded in text ("12 – 0 vote" / "vote of 8-4")
    for pattern in VOTE_TALLY_PATTERNS:
        match = pattern.search(content)
        if match is not None:
            return f"{match.group('for_votes')}-{match.group('against_votes')}"

    # Older format: members listed by name, separated by semicolons.
    # Count semicolons in the "Voting for" sentence (+1 = member count) then
    # derive against = FOMC_COMMITTEE_SIZE - for_count.
    for_match = VOTING_FOR_RE.search(content)
    if for_match is not None:
        for_count = for_match.group(1).count(";") + 1
        against_count = FOMC_COMMITTEE_SIZE - for_count
        if 0 <= against_count < FOMC_COMMITTEE_SIZE:
            return f"{for_count}-{against_count}"

    return None


def _infer_decision(content: str) -> str:
    lowered = content.lower()
    committee_decision_match = re.search(
        r"\bcommittee decided to\s+(?P<decision>maintain|maintained|keep|kept|raise|raised|increase|increased|hike|hiked|lower|lowered|reduce|reduced|cut)\b[^.]{0,200}\btarget range\b",
        lowered,
    )
    if committee_decision_match is not None:
        decision_verb = committee_decision_match.group("decision")
        if decision_verb in {"raise", "raised", "increase", "increased", "hike", "hiked"}:
            return "raise"
        if decision_verb in {"lower", "lowered", "reduce", "reduced", "cut"}:
            return "cut"
        return "hold"

    if re.search(r"\b(raise|raised|increase|increased|hike|hiked)\b[^.]{0,120}\btarget range\b", lowered):
        return "raise"
    if re.search(r"\b(lower|lowered|reduce|reduced|cut)\b[^.]{0,120}\btarget range\b", lowered):
        return "cut"
    if re.search(r"\b(maintain|maintained|keep|kept)\b[^.]{0,120}\btarget range\b", lowered):
        return "hold"
    return "hold"


def _count_terms(content: str, terms: Iterable[str]) -> int:
    return sum(content.count(term) for term in terms)


def export_dashboard_fomc_js(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    output_js_path: str | Path,
    meeting_date: str | None = None,
) -> dict[str, object] | None:
    payload = build_dashboard_fomc_payload(db_path=db_path, meeting_date=meeting_date)
    output_path = Path(output_js_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if payload is None:
        output_path.write_text("window.__FOMC_DASHBOARD_DATA__ = null;\n", encoding="utf-8")
        return None
    output_path.write_text(
        "window.__FOMC_DASHBOARD_DATA__ = " + json.dumps(payload, sort_keys=True, indent=2) + ";\n",
        encoding="utf-8",
    )
    return payload


def build_dashboard_fomc_payload(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    meeting_date: str | None = None,
) -> dict[str, object] | None:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        if meeting_date is not None:
            row = connection.execute(
                """
                SELECT meeting_date, statement_url, title, vote_tally, decision, summary, fetched_at,
                       inflation_mentions, labor_mentions, growth_mentions, content
                FROM fomc_statements
                WHERE meeting_date = ?
                ORDER BY fetched_at DESC
                LIMIT 1
                """,
                (meeting_date,),
            ).fetchone()
        else:
            row = connection.execute(
                """
                SELECT meeting_date, statement_url, title, vote_tally, decision, summary, fetched_at,
                       inflation_mentions, labor_mentions, growth_mentions, content
                FROM fomc_statements
                ORDER BY meeting_date DESC, fetched_at DESC
                LIMIT 1
                """
            ).fetchone()
    finally:
        connection.close()

    if row is None:
        return None

    raw_signal_counts = {
        "inflation_mentions": int(row["inflation_mentions"] or 0),
        "labor_mentions": int(row["labor_mentions"] or 0),
        "growth_mentions": int(row["growth_mentions"] or 0),
    }
    # Previous Meeting Bias is handled separately; the 3 economic signals are normalized against each other
    max_mentions = max(raw_signal_counts.values()) if raw_signal_counts else 0
    source_id = f"fomc_{str(row['meeting_date']).replace('-', '_')}"

    signals: list[dict[str, object]] = []
    for key, label in FOMC_SIGNAL_LABELS:
        mentions = raw_signal_counts[key]
        score = _score_signal_mentions(mentions=mentions, max_mentions=max_mentions)
        signals.append(
            {
                "label": label,
                "score": score,
                "tone": _tone_from_score(score),
                "mentions": mentions,
                "sources": [source_id, f"mentions:{mentions}"],
            }
        )

    # Previous Meeting Bias: derived from decision direction + dissenter pull, not term counting
    signals.append(
        _compute_policy_bias(
            decision=str(row["decision"] or "hold"),
            vote_tally=row["vote_tally"],
            content=str(row["content"] or ""),
            source_id=source_id,
        )
    )

    return {
        "generated_at": _utcnow().isoformat(),
        "meeting_date": row["meeting_date"],
        "meeting_label": _meeting_label_from_iso(str(row["meeting_date"])),
        "title": row["title"],
        "decision": row["decision"],
        "vote_tally": row["vote_tally"],
        "source_url": row["statement_url"],
        "summary": row["summary"],
        "fetched_at": row["fetched_at"],
        "signals": signals,
    }


def _score_signal_mentions(*, mentions: int, max_mentions: int) -> int:
    if max_mentions <= 0:
        return 3
    scaled = 1 + round((mentions / max_mentions) * 4)
    return max(1, min(5, scaled))


def _tone_from_score(score: int) -> str:
    if score >= 5:
        return "hot"
    if score == 4:
        return "warm"
    if score == 3:
        return "balanced"
    if score == 2:
        return "cool"
    return "cold"


def _compute_policy_bias(
    *,
    decision: str,
    vote_tally: str | None,
    content: str,
    source_id: str,
) -> dict[str, object]:
    """
    Score policy bias on [-1.0, +1.0]:
      base = raise → +1, hold → 0, cut → -1
      pull = (raise_dissenters - cut_dissenters) / COMMITTEE_SIZE

    Mapped to heat score 1-5: -1→1, 0→3, +1→5.
    """
    COMMITTEE_SIZE = 12
    base = {"raise": 1.0, "hold": 0.0, "cut": -1.0}.get(decision, 0.0)

    minority = 0
    if vote_tally:
        m = re.search(r"\d+\s*[-\u2013]\s*(\d+)", vote_tally)
        if m:
            minority = int(m.group(1))

    pull = 0.0
    if minority > 0:
        has_raise = bool(_DISSENT_RAISE_RE.search(content))
        has_cut = bool(_DISSENT_CUT_RE.search(content))
        if has_raise and not has_cut:
            pull = minority / COMMITTEE_SIZE
        elif has_cut and not has_raise:
            pull = -(minority / COMMITTEE_SIZE)
        # mixed / unknown → pull stays 0

    bias = round(min(1.0, max(-1.0, base + pull)), 2)

    if bias >= 0.75:
        tone = "hot"
    elif bias >= 0.25:
        tone = "warm"
    elif bias > -0.25:
        tone = "balanced"
    elif bias >= -0.75:
        tone = "cool"
    else:
        tone = "cold"

    # Align heat score directly to tone so color and number stay consistent
    heat = {"hot": 5, "warm": 4, "balanced": 3, "cool": 2, "cold": 1}[tone]

    sign = "+" if bias > 0 else ""
    label = f"{sign}{bias}"
    return {
        "label": "Previous Meeting Bias",
        "score": heat,
        "display": label,
        "tone": tone,
        "mentions": 0,
        "sources": [source_id, f"decision:{decision}", f"bias:{label}"],
    }


def _meeting_label_from_iso(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%B %Y")
    except ValueError:
        return value


def build_dashboard_fomc_history_payload(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, list[dict[str, object]]]:
    """Returns all FOMC meetings grouped by calendar year, newest first within each year."""
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT meeting_date, title, decision, vote_tally, summary, statement_url
            FROM fomc_statements
            ORDER BY meeting_date DESC
            """
        ).fetchall()
    finally:
        connection.close()

    grouped: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        year = str(row["meeting_date"])[:4]
        grouped.setdefault(year, []).append(
            {
                "meeting_date": row["meeting_date"],
                "label": _meeting_label_from_iso(str(row["meeting_date"])),
                "decision": row["decision"],
                "vote_tally": row["vote_tally"],
                "summary": row["summary"],
                "source_url": row["statement_url"],
                "title": row["title"],
            }
        )
    return grouped


def export_dashboard_fomc_history_js(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    output_js_path: str | Path,
) -> dict[str, list[dict[str, object]]]:
    payload = build_dashboard_fomc_history_payload(db_path=db_path)
    output_path = Path(output_js_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        "window.__FOMC_HISTORY_DATA__ = " + json.dumps(payload, sort_keys=True, indent=2) + ";\n",
        encoding="utf-8",
    )
    return payload


def _fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": "WhatTheFed/1.0"})
    with urlopen(request, timeout=30) as response:
        payload = response.read().decode("utf-8", errors="replace")
    return payload


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


if __name__ == "__main__":
    raise SystemExit(main())
