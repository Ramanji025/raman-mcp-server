"""Persist offline Ollama enrichment as retrievable semantic knowledge."""
from __future__ import annotations

from typing import Any

from ..config import Settings, get_settings
from ..graph.factory import get_graph_store
from ..logging import get_logger
from ..models import Chunk, ContentType
from ..vector.indexer import VectorIndexer

log = get_logger(__name__)


def _enrichment_text(record: dict[str, Any]) -> str | None:
    summary = str(record.get("llm_summary") or "").strip()
    purpose = str(record.get("llm_purpose") or record.get("llm_business_purpose") or "").strip()
    if not summary and not purpose:
        return None

    kind = str(record.get("kind") or record.get("type") or "ollama_enrichment")
    subject = str(record.get("name") or "unknown")
    lines = [
        f"Ollama Enrichment: {kind} {subject}",
        f"Service: {record.get('repo') or 'unknown'}",
    ]
    if summary:
        lines.append(f"Semantic Summary: {summary}")
    if purpose and purpose != summary:
        lines.append(f"Business Purpose: {purpose}")
    if record.get("file"):
        lines.append(f"File: {record['file']}")
    return "\n".join(lines)


def _records_from_graph(graph) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for node in graph.all_nodes():
        attrs = node.get("attributes") or {}
        summary = str(attrs.get("llm_summary") or attrs.get("llm_purpose") or "").strip()
        if not summary:
            continue
        records.append({
            "id": node.get("id"),
            "kind": node.get("type"),
            "type": node.get("type"),
            "name": node.get("name"),
            "repo": node.get("service"),
            "file": attrs.get("file"),
            "llm_summary": summary,
            "llm_purpose": attrs.get("llm_purpose") or summary,
        })
    return records


def sync_ollama_enrichment(settings: Settings | None = None) -> int:
    """Index Neo4j Ollama enrichments into the semantic retrieval collection."""
    settings = settings or get_settings()
    graph = get_graph_store(settings)
    graph.load()
    records = _records_from_graph(graph)
    if not records:
        log.info("ollama_enrichment_sync_skipped", reason="no llm_summary on graph nodes")
        return 0

    chunks: list[Chunk] = []
    seen: set[str] = set()
    for record in records:
        text = _enrichment_text(record)
        if not text:
            continue
        record_id = str(record.get("id") or "")
        if not record_id:
            continue
        chunk_id = f"ollama:{record_id}"
        if chunk_id in seen:
            continue
        seen.add(chunk_id)
        repo = str(record.get("repo") or "platform")
        rel_path = str(record.get("file") or f"neo4j://{record_id}")
        chunks.append(
            Chunk(
                id=chunk_id,
                repo=repo,
                rel_path=rel_path,
                content_type=ContentType.SEMANTIC,
                collection="semantic",
                text=text,
                metadata={
                    "service": repo,
                    "kind": "ollama_enrichment",
                    "enrichment_kind": record.get("kind", "ollama_enrichment"),
                    "node_id": record_id,
                    "symbol": record.get("name") or "",
                    "source": "neo4j_llm_enrichment",
                },
            )
        )

    written = VectorIndexer(settings).index(chunks)
    log.info("ollama_enrichment_synced", records=len(records), chunks=written)
    return written
