from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Iterable

from .rag import Document


TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9\-']*")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
VOTE_TALLY_RE = re.compile(r"\b(?P<for>\d+)\s*[-–]\s*(?P<against>\d+)\s+vote\b", re.IGNORECASE)
VOTING_FOR_RE = re.compile(r"Voting for [^.]* were (?P<names>.+?)\.", re.IGNORECASE | re.DOTALL)
VOTING_AGAINST_RE = re.compile(r"Voting against [^.]* (?:was|were) (?P<names>.+?)\.", re.IGNORECASE | re.DOTALL)
PERSON_NAME_RE = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z]\.)*(?:\s+[A-Z][a-z]+)+\b")

TOPIC_KEYWORDS = {
    "inflation": ("inflation", "price stability", "prices", "core services"),
    "labor": ("labor", "employment", "hiring", "unemployment", "job gains"),
    "growth": ("growth", "activity", "investment", "demand", "productivity"),
    "policy": ("target range", "federal funds rate", "policy", "rates"),
    "risk": ("uncertainty", "risk", "supply shock", "conflict", "geopolitical"),
    "market-implied-path": ("probability", "implied", "odds", "basis points"),
}
MEETING_KINDS = frozenset({"meeting_note", "fomc_statement", "fomc_minutes"})
MARKET_KINDS = frozenset({"kalshi_market", "polymarket_market", "market_signal"})
CPI_KINDS = frozenset({"cpi_observation", "cpi_metric", "labor_observation", "labor_metric"})
EXCLUDED_NAME_MATCHES = {"Vice Chair"}


@dataclass(frozen=True)
class GraphChunk:
    id: str
    document_source: str
    order: int
    text: str
    token_count: int
    kind: str
    meeting_date: str | None = None
    published_at: str | None = None
    source_url: str | None = None


@dataclass(frozen=True)
class GraphNode:
    id: str
    kind: str
    label: str
    properties: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class GraphEdge:
    source: str
    target: str
    relation: str
    properties: dict[str, object] = field(default_factory=dict)


@dataclass
class KnowledgeGraph:
    nodes: dict[str, GraphNode] = field(default_factory=dict)
    edges: list[GraphEdge] = field(default_factory=list)
    chunks: list[GraphChunk] = field(default_factory=list)

    def add_node(self, node: GraphNode) -> None:
        self.nodes[node.id] = node

    def add_edge(self, edge: GraphEdge) -> None:
        self.edges.append(edge)

    def add_chunk(self, chunk: GraphChunk) -> None:
        self.chunks.append(chunk)
        self.add_node(
            GraphNode(
                id=chunk.id,
                kind="chunk",
                label=f"{chunk.document_source} chunk {chunk.order + 1}",
                properties={
                    "document_source": chunk.document_source,
                    "order": chunk.order,
                    "text": chunk.text,
                    "token_count": chunk.token_count,
                    "kind": chunk.kind,
                    "meeting_date": chunk.meeting_date,
                    "published_at": chunk.published_at,
                    "source_url": chunk.source_url,
                },
            )
        )

    def nodes_by_kind(self, kind: str) -> list[GraphNode]:
        return [node for node in self.nodes.values() if node.kind == kind]

    def neighbors(self, node_id: str, relation: str | None = None) -> list[GraphNode]:
        related_node_ids = [
            edge.target
            for edge in self.edges
            if edge.source == node_id and (relation is None or edge.relation == relation)
        ]
        return [self.nodes[target_id] for target_id in related_node_ids if target_id in self.nodes]

    def to_dict(self) -> dict[str, object]:
        return {
            "nodes": [asdict(node) for node in self.nodes.values()],
            "edges": [asdict(edge) for edge in self.edges],
            "chunks": [asdict(chunk) for chunk in self.chunks],
        }


class KnowledgeGraphBuilder:
    def __init__(self, chunk_token_target: int = 220, chunk_overlap_tokens: int = 40) -> None:
        if chunk_token_target <= 0:
            raise ValueError("chunk_token_target must be positive.")
        if chunk_overlap_tokens < 0:
            raise ValueError("chunk_overlap_tokens cannot be negative.")
        self.chunk_token_target = chunk_token_target
        self.chunk_overlap_tokens = chunk_overlap_tokens

    def build(self, documents: Iterable[Document]) -> KnowledgeGraph:
        graph = KnowledgeGraph()
        for document in documents:
            self._add_document(graph, document)
        return graph

    def _add_document(self, graph: KnowledgeGraph, document: Document) -> None:
        document_id = f"document:{document.source}"
        graph.add_node(
            GraphNode(
                id=document_id,
                kind="document",
                label=document.source,
                properties={
                    "source": document.source,
                    "kind": document.kind,
                    "meeting_date": document.meeting_date,
                    "published_at": document.published_at,
                    "source_url": document.source_url,
                    "metadata": dict(document.metadata),
                },
            )
        )

        for index, chunk_text in enumerate(self._chunk_text(document.content)):
            chunk = GraphChunk(
                id=f"chunk:{document.source}:{index + 1}",
                document_source=document.source,
                order=index,
                text=chunk_text,
                token_count=len(_tokenize(chunk_text)),
                kind=document.kind,
                meeting_date=document.meeting_date,
                published_at=document.published_at,
                source_url=document.source_url,
            )
            graph.add_chunk(chunk)
            graph.add_edge(GraphEdge(source=document_id, target=chunk.id, relation="has_chunk"))
            self._add_topic_links(graph, chunk.id, chunk_text)

        if document.kind in MEETING_KINDS:
            self._add_meeting_structure(graph, document_id, document)
        if document.kind in MARKET_KINDS:
            self._add_market_structure(graph, document_id, document)
        if document.kind in CPI_KINDS:
            self._add_cpi_structure(graph, document_id, document)

    def _add_meeting_structure(self, graph: KnowledgeGraph, document_id: str, document: Document) -> None:
        meeting_id = self._meeting_node_id(document)
        graph.add_node(
            GraphNode(
                id=meeting_id,
                kind="meeting",
                label=self._meeting_label(document),
                properties={
                    "meeting_date": document.meeting_date,
                    "source": document.source,
                    "source_url": document.source_url,
                },
            )
        )
        graph.add_edge(GraphEdge(source=document_id, target=meeting_id, relation="describes_meeting"))

        decision = self._infer_decision(document.content)
        decision_id = f"decision:{meeting_id}"
        graph.add_node(
            GraphNode(
                id=decision_id,
                kind="policy_decision",
                label=decision.upper(),
                properties={"decision": decision},
            )
        )
        graph.add_edge(GraphEdge(source=meeting_id, target=decision_id, relation="has_policy_decision"))

        vote_summary = self._extract_vote_summary(document.content)
        if vote_summary is not None:
            vote_id = f"vote:{meeting_id}"
            graph.add_node(
                GraphNode(
                    id=vote_id,
                    kind="meeting_vote",
                    label=vote_summary["official_tally"],
                    properties=vote_summary,
                )
            )
            graph.add_edge(GraphEdge(source=meeting_id, target=vote_id, relation="has_vote_summary"))

    def _add_market_structure(self, graph: KnowledgeGraph, document_id: str, document: Document) -> None:
        provider = str(document.metadata.get("provider", document.kind.replace("_market", "").replace("_", " "))).title()
        market_id = f"market:{document.source}"
        graph.add_node(
            GraphNode(
                id=market_id,
                kind="market",
                label=str(document.metadata.get("market_name", document.source)),
                properties={
                    "provider": provider,
                    "source": document.source,
                    "source_url": document.source_url,
                    "metadata": dict(document.metadata),
                },
            )
        )
        graph.add_edge(GraphEdge(source=document_id, target=market_id, relation="describes_market"))

        snapshot_id = f"snapshot:{document.source}:{document.published_at or 'latest'}"
        snapshot_properties = {
            "provider": provider,
            "published_at": document.published_at,
            "target_meeting": document.metadata.get("target_meeting"),
            "raise_probability": document.metadata.get("raise_probability"),
            "hold_probability": document.metadata.get("hold_probability"),
            "cut_probability": document.metadata.get("cut_probability"),
            "volume": document.metadata.get("volume"),
            "liquidity": document.metadata.get("liquidity"),
        }
        graph.add_node(
            GraphNode(
                id=snapshot_id,
                kind="market_snapshot",
                label=f"{provider} snapshot",
                properties=snapshot_properties,
            )
        )
        graph.add_edge(GraphEdge(source=market_id, target=snapshot_id, relation="has_market_snapshot"))

        target_meeting = document.metadata.get("target_meeting")
        if isinstance(target_meeting, str) and target_meeting:
            meeting_id = f"meeting:{target_meeting}"
            graph.add_node(
                GraphNode(
                    id=meeting_id,
                    kind="meeting",
                    label=target_meeting,
                    properties={"meeting_key": target_meeting},
                )
            )
            graph.add_edge(GraphEdge(source=snapshot_id, target=meeting_id, relation="targets_meeting"))

        self._add_topic_links(graph, snapshot_id, document.content)

    def _add_cpi_structure(self, graph: KnowledgeGraph, document_id: str, document: Document) -> None:
        metadata = dict(document.metadata)
        series_id = str(metadata.get("series_id") or document.source)
        series_label = str(metadata.get("series_label") or series_id)
        observation_date = str(metadata.get("observation_date") or document.published_at or "unknown")
        metric_namespace = "labor" if document.kind.startswith("labor_") else "cpi"
        category = str(metadata.get("category") or metric_namespace)

        series_node_id = f"{metric_namespace}_series:{series_id}"
        graph.add_node(
            GraphNode(
                id=series_node_id,
                kind=f"{metric_namespace}_series",
                label=series_label,
                properties={
                    "series_id": series_id,
                    "category": category,
                },
            )
        )
        graph.add_edge(
            GraphEdge(source=document_id, target=series_node_id, relation=f"describes_{metric_namespace}_series")
        )

        observation_node_id = f"{metric_namespace}_observation:{series_id}:{observation_date}"
        graph.add_node(
            GraphNode(
                id=observation_node_id,
                kind=f"{metric_namespace}_observation",
                label=observation_date,
                properties={
                    "series_id": series_id,
                    "observation_date": observation_date,
                    "value": metadata.get("value"),
                    "source_url": document.source_url,
                },
            )
        )
        graph.add_edge(
            GraphEdge(source=series_node_id, target=observation_node_id, relation=f"has_{metric_namespace}_observation")
        )

        metric_key = metadata.get("metric_key")
        if metric_key is not None:
            metric_node_id = f"{metric_namespace}_metric:{metric_key}:{observation_date}"
            graph.add_node(
                GraphNode(
                    id=metric_node_id,
                    kind=f"{metric_namespace}_metric",
                    label=str(metric_key),
                    properties={
                        "metric_key": metric_key,
                        "metric_value": metadata.get("metric_value"),
                        "metric_date": observation_date,
                    },
                )
            )
            graph.add_edge(
                GraphEdge(
                    source=observation_node_id,
                    target=metric_node_id,
                    relation="observation_contributes_to_metric",
                )
            )

    def _add_topic_links(self, graph: KnowledgeGraph, source_id: str, text: str) -> None:
        lowered = text.lower()
        for topic, keywords in TOPIC_KEYWORDS.items():
            if any(keyword in lowered for keyword in keywords):
                topic_id = f"topic:{topic}"
                graph.add_node(GraphNode(id=topic_id, kind="topic", label=topic.replace("-", " ").title()))
                graph.add_edge(GraphEdge(source=source_id, target=topic_id, relation="mentions_topic"))

    def _chunk_text(self, text: str) -> list[str]:
        units = [unit for unit in self._split_units(text) if unit]
        if not units:
            stripped = text.strip()
            return [stripped] if stripped else []

        chunks: list[str] = []
        start = 0
        while start < len(units):
            end = start
            token_total = 0
            while end < len(units):
                token_total += len(_tokenize(units[end]))
                end += 1
                if token_total >= self.chunk_token_target:
                    break

            chunk_text = " ".join(units[start:end]).strip()
            if chunk_text:
                chunks.append(chunk_text)

            if end >= len(units):
                break

            overlap_start = end
            overlap_tokens = 0
            while overlap_start - 1 > start and overlap_tokens < self.chunk_overlap_tokens:
                overlap_start -= 1
                overlap_tokens += len(_tokenize(units[overlap_start]))
            start = overlap_start if overlap_start > start else end

        return chunks

    @staticmethod
    def _split_units(text: str) -> list[str]:
        paragraphs = [part.strip() for part in text.split("\n\n") if part.strip()]
        units: list[str] = []
        for paragraph in paragraphs:
            sentences = [sentence.strip() for sentence in SENTENCE_SPLIT_RE.split(paragraph) if sentence.strip()]
            units.extend(sentences or [paragraph])
        return units

    @staticmethod
    def _meeting_node_id(document: Document) -> str:
        return f"meeting:{document.meeting_date or document.source}"

    @staticmethod
    def _meeting_label(document: Document) -> str:
        if document.meeting_date:
            try:
                return f"{datetime.fromisoformat(document.meeting_date):%B %Y} Meeting"
            except ValueError:
                pass
        return document.source.replace("_", " ").title()

    @staticmethod
    def _infer_decision(text: str) -> str:
        lowered = text.lower()
        if any(word in lowered for word in ("raise", "raised", "hike", "increase")):
            return "raise"
        if any(word in lowered for word in ("cut", "reduced", "lowered")):
            return "cut"
        return "hold"

    def _extract_vote_summary(self, text: str) -> dict[str, object] | None:
        tally_match = VOTE_TALLY_RE.search(text)
        if tally_match is not None:
            votes_for = int(tally_match.group("for"))
            votes_against = int(tally_match.group("against"))
            return {
                "votes_for": votes_for,
                "votes_against": votes_against,
                "official_tally": f"{votes_for}-{votes_against}",
                "inference": "explicit_tally",
            }

        names_for = self._extract_named_voter_count(VOTING_FOR_RE, text)
        names_against = self._extract_named_voter_count(VOTING_AGAINST_RE, text)
        if names_for is None and names_against is None:
            return None

        votes_for = names_for or 0
        votes_against = names_against or 0
        return {
            "votes_for": votes_for,
            "votes_against": votes_against,
            "official_tally": f"{votes_for}-{votes_against}",
            "inference": "parsed_voter_lists",
        }

    @staticmethod
    def _extract_named_voter_count(pattern: re.Pattern[str], text: str) -> int | None:
        match = pattern.search(text)
        if match is None:
            return None
        names = {
            name.strip()
            for name in PERSON_NAME_RE.findall(match.group("names"))
            if name.strip() not in EXCLUDED_NAME_MATCHES
        }
        return len(names)


def _tokenize(text: str) -> list[str]:
    return [token.lower() for token in TOKEN_RE.findall(text)]
