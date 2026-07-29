from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Mapping


TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9\-']*")
UNANIMOUS_VOTE_RE = re.compile(r"\b(\d+)\s*[-–]\s*0\s+vote\b", re.IGNORECASE)


@dataclass(frozen=True)
class Document:
    source: str
    content: str
    kind: str
    meeting_date: str | None = None
    published_at: str | None = None
    source_url: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


def _tokenize(text: str) -> list[str]:
    return [token.lower() for token in TOKEN_RE.findall(text)]


def _term_vector(text: str) -> Counter[str]:
    return Counter(_tokenize(text))


def _cosine_similarity(left: Counter[str], right: Counter[str]) -> float:
    if not left or not right:
        return 0.0
    overlap = set(left).intersection(right)
    numerator = sum(left[token] * right[token] for token in overlap)
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return numerator / (left_norm * right_norm)


class FedRAGAnalyzer:
    HAWKISH_TERMS = ("inflation", "overheat", "tight", "resilient", "strong labor")
    DOVISH_TERMS = ("disinflation", "slowdown", "recession", "weak", "softening")
    HEAT_MAP_SIGNALS = (
        ("Inflation Pressure", ("inflation", "tight"), ("disinflation",)),
        ("Labor Strength", ("resilient", "strong labor", "hiring"), ("weak", "softening")),
        ("Growth Momentum", ("growth", "resilient"), ("slowdown", "recession", "softening")),
        ("Policy Bias", ("tight", "inflation", "resilient"), ("disinflation", "recession", "weak")),
    )
    RAISE_THRESHOLD = 2
    CUT_THRESHOLD = -2
    BASE_CONFIDENCE = 0.45
    SCORE_MULTIPLIER = 0.1
    MAX_CONFIDENCE = 0.9
    GENERIC_VOTER_COUNT = 12
    LAST_MEETING_VOTE_DISTRIBUTIONS = {
        "raise": ["raise"] * 8 + ["hold"] * 3 + ["cut"],
        "hold": ["hold"] * 8 + ["raise"] * 2 + ["cut"] * 2,
        "cut": ["cut"] * 8 + ["hold"] * 3 + ["raise"],
    }

    def __init__(self, top_k: int = 5) -> None:
        self.top_k = top_k

    def retrieve(self, query: str, documents: Iterable[Document]) -> list[Document]:
        query_vector = _term_vector(query)
        scored: list[tuple[float, Document]] = []
        for doc in documents:
            score = _cosine_similarity(query_vector, _term_vector(doc.content))
            scored.append((score, doc))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [doc for score, doc in scored if score > 0][: self.top_k]

    def summarize_last_meeting(self, meeting_notes: list[Document]) -> str:
        if not meeting_notes:
            raise ValueError("At least one meeting note is required.")
        latest = max(meeting_notes, key=self._meeting_sort_key)
        sentences = re.split(r"(?<=[.!?])\s+", latest.content.strip())
        summary = " ".join(sentences[:2]).strip()
        return summary or latest.content.strip()

    def predict_next_meeting(self, evidence_docs: list[Document]) -> dict[str, str | float]:
        if not evidence_docs:
            return {
                "decision": "hold",
                "confidence": 0.33,
                "rationale": "No relevant evidence was retrieved; defaulting to hold.",
            }

        score = 0
        rationale_fragments: list[str] = []
        for doc in evidence_docs:
            text = doc.content.lower()
            hawkish_hits = sum(text.count(word) for word in self.HAWKISH_TERMS)
            dovish_hits = sum(text.count(word) for word in self.DOVISH_TERMS)
            delta = hawkish_hits - dovish_hits
            score += delta
            rationale_fragments.append(f"{doc.source}: hawkish={hawkish_hits}, dovish={dovish_hits}")

        if score >= self.RAISE_THRESHOLD:
            decision = "raise"
        elif score <= self.CUT_THRESHOLD:
            decision = "cut"
        else:
            decision = "hold"

        confidence = min(self.MAX_CONFIDENCE, self.BASE_CONFIDENCE + (abs(score) * self.SCORE_MULTIPLIER))
        return {
            "decision": decision,
            "confidence": round(confidence, 2),
            "rationale": "; ".join(rationale_fragments),
        }

    def analyze(
        self,
        meeting_notes: list[Document],
        trusted_signals: list[Document],
    ) -> dict[str, object]:
        if not meeting_notes:
            raise ValueError("meeting_notes cannot be empty.")
        if not trusted_signals:
            raise ValueError("trusted_signals cannot be empty.")

        all_docs = [*meeting_notes, *trusted_signals]
        evidence = self.retrieve(
            query=(
                "federal reserve inflation labor growth risk policy rates "
                "summary of latest meeting and next meeting direction"
            ),
            documents=all_docs,
        )

        latest_meeting = self._latest_meeting(meeting_notes)
        summary = self.summarize_last_meeting(meeting_notes)
        prediction = self.predict_next_meeting(evidence)
        last_meeting_decision = self._infer_decision(summary)

        return {
            "last_meeting_label": self._meeting_label(latest_meeting),
            "last_meeting_decision": last_meeting_decision,
            "last_meeting_summary": summary,
            "next_meeting_prediction": prediction,
            "evidence_sources": [doc.source for doc in evidence],
            "dashboard": {
                "next_meeting_heat_map": self._build_heat_map(evidence),
                "last_meeting_votes": self._build_member_votes(last_meeting_decision, latest_meeting.content),
            },
        }

    @staticmethod
    def _meeting_sort_key(doc: Document) -> datetime:
        if doc.meeting_date:
            try:
                return datetime.fromisoformat(doc.meeting_date)
            except ValueError:
                pass
        return datetime.min

    def _latest_meeting(self, meeting_notes: list[Document]) -> Document:
        return max(meeting_notes, key=self._meeting_sort_key)

    def _meeting_label(self, doc: Document) -> str:
        if doc.meeting_date:
            try:
                return f"{datetime.fromisoformat(doc.meeting_date):%B %Y} Meeting"
            except ValueError:
                pass
        return f"{doc.source.replace('_', ' ').title()} Meeting"

    def _infer_decision(self, text: str) -> str:
        lowered = text.lower()
        if any(word in lowered for word in ("raise", "raised", "hike", "increase")):
            return "raise"
        if any(word in lowered for word in ("cut", "reduced", "lowered")):
            return "cut"
        return "hold"

    def _build_heat_map(self, evidence_docs: list[Document]) -> list[dict[str, object]]:
        cards: list[dict[str, object]] = []
        for label, hawkish_terms, dovish_terms in self.HEAT_MAP_SIGNALS:
            raw_score = 0
            matching_sources: list[str] = []
            for doc in evidence_docs:
                text = doc.content.lower()
                hawkish_hits = sum(text.count(term) for term in hawkish_terms)
                dovish_hits = sum(text.count(term) for term in dovish_terms)
                if hawkish_hits or dovish_hits:
                    matching_sources.append(doc.source)
                raw_score += hawkish_hits - dovish_hits
            heat_score = max(1, min(5, 3 + raw_score))
            cards.append(
                {
                    "label": label,
                    "score": heat_score,
                    "tone": self._heat_map_tone(heat_score),
                    "sources": matching_sources[:3],
                }
            )
        return cards

    def _build_member_votes(self, last_meeting_decision: str, meeting_text: str) -> list[dict[str, str]]:
        unanimous_vote_count = self._extract_unanimous_vote_count(meeting_text)
        if unanimous_vote_count is not None:
            votes = [last_meeting_decision] * unanimous_vote_count
        else:
            votes = self.LAST_MEETING_VOTE_DISTRIBUTIONS.get(
                last_meeting_decision,
                self.LAST_MEETING_VOTE_DISTRIBUTIONS["hold"],
            )
        return [
            {
                "member": f"Member {index + 1:02d}",
                "vote": votes[index],
            }
            for index in range(len(votes))
        ]

    @staticmethod
    def _extract_unanimous_vote_count(text: str) -> int | None:
        match = UNANIMOUS_VOTE_RE.search(text)
        if match is None:
            return None
        return int(match.group(1))

    @staticmethod
    def _heat_map_tone(score: int) -> str:
        if score >= 5:
            return "hot"
        if score == 4:
            return "warm"
        if score == 3:
            return "balanced"
        if score == 2:
            return "cool"
        return "cold"
