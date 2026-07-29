# WhatTheFed

Minimal retrieval-augmented analysis for FED meetings and trusted macro signals.

## What it does

- Ingests FED meeting notes and trusted signal documents
- Retrieves the most relevant evidence for a summary/prediction request
- Summarizes the latest meeting note
- Predicts the next FED move (`raise`, `hold`, or `cut`) from retrieved evidence
- Exposes dashboard-friendly data for a static HTML meeting page
- Provides a backend knowledge-graph pipeline for meetings, votes, topics, and market snapshots

## Architecture

![Architecture diagram](assets/architecturediagram.png)

## Quick usage

```python
from whatthefed.rag import Document, FedRAGAnalyzer

meeting_notes = [
    Document(
        source="fomc_2026_06",
        content="Committee kept rates unchanged while noting persistent inflation risks.",
        kind="meeting_note",
        meeting_date="2026-06-17",
    )
]

trusted_signals = [
    Document(source="cpi", content="Core CPI remains elevated and broad-based.", kind="trusted_signal"),
    Document(source="jobs", content="Labor market remains resilient with low unemployment.", kind="trusted_signal"),
]

analyzer = FedRAGAnalyzer()
report = analyzer.analyze(meeting_notes=meeting_notes, trusted_signals=trusted_signals)
print(report["last_meeting_summary"])
print(report["next_meeting_prediction"])
print(report["dashboard"]["next_meeting_heat_map"])
```

## Dashboard

Open `./index.html` in a browser to view a static dashboard prototype with:

- a next-meeting heat map
- a scrollable previous-meeting history with direct links to each official Fed HTML statement

The page is wired to the same top-level data shape returned by `FedRAGAnalyzer.analyze(...)`, so the inline demo payload can later be replaced with serialized RAG output.

![Dashboard preview](assets/dashboard-preview.png)

## Knowledge graph backend

The backend can now build a hybrid graph from source documents:

- **documents** preserve the original FOMC statement, macro release, or market source
- **chunks** keep retrieval-friendly sentence groups instead of tiny word fragments
- **nodes and edges** capture relationships among meetings, policy decisions, vote summaries, topics, and market snapshots
- **market snapshots** can represent daily Kalshi or Polymarket data linked to a target meeting

Example:

```python
from whatthefed import Document, KnowledgeGraphBuilder

documents = [
    Document(
        source="fomc_2026_06",
        content=(
            "The Federal Open Market Committee approved the following statement for release by a 12-0 vote. "
            "The Committee decided to maintain the target range for the federal funds rate at 3-1/2 to 3-3/4 percent."
        ),
        kind="meeting_note",
        meeting_date="2026-06-17",
        source_url="https://www.federalreserve.gov/newsevents/pressreleases/monetary20260617a.htm",
    ),
    Document(
        source="kalshi_sep_2026",
        content="Kalshi implied odds for the September 2026 FOMC meeting show a high hold probability.",
        kind="kalshi_market",
        published_at="2026-07-28T00:00:00Z",
        metadata={
            "provider": "Kalshi",
            "target_meeting": "2026-09-16",
            "raise_probability": 0.12,
            "hold_probability": 0.73,
            "cut_probability": 0.15,
        },
    ),
]

graph = KnowledgeGraphBuilder().build(documents)
print(graph.to_dict())
```

This gives the RAG layer both retrieval text and graph structure, which is a better fit for meeting-history questions, dissent analysis, and blending Fed language with market-implied expectations.

## Market ingestion, scheduling, and storage

Kalshi and Polymarket ingestion is set up as a **watchlist-driven backend pipeline**:

- `KalshiMarketClient` fetches a configured Kalshi market from the public trade API
- `KalshiEventClient` fetches all child markets under a Kalshi event ticker (useful for `kxfeddecision` and `kxfed`)
- `PolymarketMarketClient` fetches a configured Polymarket market by slug from the Gamma API
- `PolymarketEventClient` fetches all child markets under a Polymarket event slug
- `MarketIngestionService` runs the watchlist and writes snapshots into SQLite
- `MarketSnapshotStore` persists:
  - one row per market snapshot
  - one row per outcome inside that snapshot
  - one row per ingestion run for basic auditability

The default SQLite location is:

```text
data/market_snapshots.db
```

Each watchlist entry tells the ingester which provider/market to fetch and, when useful, how to map outcomes to canonical labels like `raise`, `hold`, or `cut`.

Example watchlist JSON:

```json
[
  {
    "provider": "kalshi_event",
    "market_ref": "KXFEDDECISION-26SEP",
    "target_meeting": "2026-09-16",
    "metadata": {
      "market_family": "kxfeddecision"
    }
  },
  {
    "provider": "kalshi_event",
    "market_ref": "KXFED-26SEP",
    "target_meeting": "2026-09-16",
    "metadata": {
      "market_family": "kxfed"
    }
  },
  {
    "provider": "polymarket_event",
    "market_ref": "fed-decision-in-september-762",
    "target_meeting": "2026-09-16",
    "source_url": "https://polymarket.com/event/fed-decision-in-september-762"
  }
]
```

Run ingestion manually with:

```bash
python -m whatthefed.market_ingestion --watchlist config/market_watchlist.json
```

### Recommended scheduling

For daily updates, the simplest path is:

1. store the watchlist JSON in the repo or deployment environment
2. run `python -m whatthefed.market_ingestion --watchlist ...` once per day
3. load stored snapshots back into `Document` objects with `MarketSnapshotStore.load_documents(...)`
4. feed those market documents into `KnowledgeGraphBuilder` alongside FOMC and macro documents

You can schedule that command with:

- a local OS scheduler such as **Task Scheduler** or cron
- a CI scheduler such as **GitHub Actions cron**
- an app-level scheduled workflow that runs the same command daily

The important architectural choice is that **SQLite is the source of truth for market snapshots**, while the knowledge graph is rebuilt from stored documents and snapshots as needed. That keeps ingestion idempotent, gives you time-series history, and makes it easy to compare market-implied odds across days leading into each FOMC meeting.

### Windows Task Scheduler (local daily run)

This repo includes:

- `config/market_watchlist.json` (starter watchlist)
- `scripts/run-market-ingestion.ps1` (one ingestion run + logging)
- `scripts/register-market-ingestion-task.ps1` (register/update scheduled task)

Register the 8:00 AM daily task:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\register-market-ingestion-task.ps1 -RunAt 08:00
```

Manual smoke run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-market-ingestion.ps1
```