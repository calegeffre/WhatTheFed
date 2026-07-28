# WhatTheFed

Minimal retrieval-augmented analysis for FED meetings and trusted macro signals.

## What it does

- Ingests FED meeting notes and trusted signal documents
- Retrieves the most relevant evidence for a summary/prediction request
- Summarizes the latest meeting note
- Predicts the next FED move (`raise`, `hold`, or `cut`) from retrieved evidence

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
```