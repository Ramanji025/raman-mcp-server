"""CLI wrapper for Neo4j-backed Java/Spring LLM enrichment.

Persists summaries on graph nodes (classes, methods, parameters, fields,
annotations, logical blocks) and indexes them in Qdrant. Does not write
enrichment payloads to JSON files.

    .venv\\Scripts\\python.exe scripts\\llm_enrich_full.py
    .venv\\Scripts\\python.exe scripts\\llm_enrich_full.py --workers 4
    .venv\\Scripts\\python.exe -m mcp_kb.ingestion.llm_enrichment --repo dic-store-service
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from mcp_kb.ingestion.llm_enrichment import cli_main  # noqa: E402

if __name__ == "__main__":
    cli_main()
