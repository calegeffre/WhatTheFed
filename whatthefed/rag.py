from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable


TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9\-']*")


@dataclass(frozen=True)
class Document:
    source: str
    content: str
    kind: str
    meeting_date: str | None = None


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
            hawkish_hits = sum(
                text.count(word)
                for word in ("inflation", "overheat", "tight", "resilient", "strong labor")
            )
            dovish_hits = sum(
                text.count(word) for word in ("disinflation", "slowdown", "recession", "weak", "softening")
            )
            delta = hawkish_hits - dovish_hits
            score += delta
            rationale_fragments.append(f"{doc.source}: hawkish={hawkish_hits}, dovish={dovish_hits}")

        if score >= 2:
            decision = "raise"
        elif score <= -2:
            decision = "cut"
        else:
            decision = "hold"

        confidence = min(0.9, 0.45 + (abs(score) * 0.1))
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

        summary = self.summarize_last_meeting(meeting_notes)
        prediction = self.predict_next_meeting(evidence)

        return {
            "last_meeting_summary": summary,
            "next_meeting_prediction": prediction,
            "evidence_sources": [doc.source for doc in evidence],
        }

    @staticmethod
    def _meeting_sort_key(doc: Document) -> datetime:
        if doc.meeting_date:
            try:
                return datetime.fromisoformat(doc.meeting_date)
            except ValueError:
                pass
        return datetime.min
