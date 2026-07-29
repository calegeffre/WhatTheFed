from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "Document",
    "FedRAGAnalyzer",
    "build_dashboard_fomc_payload",
    "FOMCIngestionService",
    "FOMCStatement",
    "FOMCStatementStore",
    "export_dashboard_fomc_js",
    "GraphChunk",
    "GraphEdge",
    "GraphNode",
    "KnowledgeGraph",
    "KnowledgeGraphBuilder",
    "KalshiEventClient",
    "KalshiMarketClient",
    "MarketIngestionService",
    "MarketOutcome",
    "MarketSnapshot",
    "MarketSnapshotStore",
    "MarketWatchConfig",
    "PolymarketEventClient",
    "PolymarketMarketClient",
    "build_dashboard_market_payload",
    "export_dashboard_market_js",
    "load_watchlist",
    "parse_calendar_statement_urls",
    "parse_statement_html",
    "snapshot_to_document",
]

_EXPORTS: dict[str, tuple[str, str]] = {
    "Document": ("rag", "Document"),
    "FedRAGAnalyzer": ("rag", "FedRAGAnalyzer"),
    "build_dashboard_fomc_payload": ("fomc_ingestion", "build_dashboard_fomc_payload"),
    "FOMCIngestionService": ("fomc_ingestion", "FOMCIngestionService"),
    "FOMCStatement": ("fomc_ingestion", "FOMCStatement"),
    "FOMCStatementStore": ("fomc_ingestion", "FOMCStatementStore"),
    "export_dashboard_fomc_js": ("fomc_ingestion", "export_dashboard_fomc_js"),
    "GraphChunk": ("knowledge_graph", "GraphChunk"),
    "GraphEdge": ("knowledge_graph", "GraphEdge"),
    "GraphNode": ("knowledge_graph", "GraphNode"),
    "KnowledgeGraph": ("knowledge_graph", "KnowledgeGraph"),
    "KnowledgeGraphBuilder": ("knowledge_graph", "KnowledgeGraphBuilder"),
    "KalshiEventClient": ("market_ingestion", "KalshiEventClient"),
    "KalshiMarketClient": ("market_ingestion", "KalshiMarketClient"),
    "MarketIngestionService": ("market_ingestion", "MarketIngestionService"),
    "MarketOutcome": ("market_ingestion", "MarketOutcome"),
    "MarketSnapshot": ("market_ingestion", "MarketSnapshot"),
    "MarketSnapshotStore": ("market_ingestion", "MarketSnapshotStore"),
    "MarketWatchConfig": ("market_ingestion", "MarketWatchConfig"),
    "PolymarketEventClient": ("market_ingestion", "PolymarketEventClient"),
    "PolymarketMarketClient": ("market_ingestion", "PolymarketMarketClient"),
    "build_dashboard_market_payload": ("market_ingestion", "build_dashboard_market_payload"),
    "export_dashboard_market_js": ("market_ingestion", "export_dashboard_market_js"),
    "load_watchlist": ("market_ingestion", "load_watchlist"),
    "parse_calendar_statement_urls": ("fomc_ingestion", "parse_calendar_statement_urls"),
    "parse_statement_html": ("fomc_ingestion", "parse_statement_html"),
    "snapshot_to_document": ("market_ingestion", "snapshot_to_document"),
}


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(f"module 'whatthefed' has no attribute {name!r}")
    module_name, attr_name = _EXPORTS[name]
    module = import_module(f".{module_name}", __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
