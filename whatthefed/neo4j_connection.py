"""Server-side Aura connection settings; never exported to the dashboard."""

from __future__ import annotations

import os
from pathlib import Path


def load_settings(path: Path | None = None) -> dict[str, str]:
    settings: dict[str, str] = {}
    if path is not None and path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if not separator or not key.strip().startswith("NEO4J_"):
                raise ValueError("Invalid Neo4j settings file; expected NEO4J_KEY=value lines.")
            settings[key.strip()] = value.strip().strip("'\"")
    for key in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD", "NEO4J_DATABASE"):
        if os.environ.get(key):
            settings[key] = os.environ[key]
    settings.setdefault("NEO4J_DATABASE", "neo4j")
    missing = [
        key
        for key in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD")
        if not settings.get(key)
    ]
    if missing:
        raise ValueError(f"Missing Neo4j settings: {', '.join(missing)}.")
    if not settings["NEO4J_URI"].startswith("neo4j+s://"):
        raise ValueError("Use an encrypted neo4j+s:// URI for Aura.")
    return settings


def connect(settings: dict[str, str]):
    from neo4j import GraphDatabase

    return GraphDatabase.driver(
        settings["NEO4J_URI"],
        auth=(settings["NEO4J_USERNAME"], settings["NEO4J_PASSWORD"]),
        connection_timeout=20,
    )
