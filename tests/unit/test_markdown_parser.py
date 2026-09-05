"""Unit tests for the markdown / incident parser."""
from __future__ import annotations

from pathlib import Path

from mcp_kb.ingestion.parsers.markdown_parser import MarkdownParser
from mcp_kb.models import ContentType, NodeType, SourceFile


def _source(repo_root: Path, rel: str, ctype: ContentType) -> SourceFile:
    path = repo_root / rel
    return SourceFile(repo="rx-order-service", rel_path=rel, abs_path=str(path),
                      content_type=ctype, sha256="x", size_bytes=path.stat().st_size)


def test_incident_becomes_node_with_affected_services(settings, fixture_repo):
    parser = MarkdownParser(settings, ContentType.INCIDENTS)
    src = _source(fixture_repo, "docs/incidents/INC-2043.md", ContentType.INCIDENTS)
    result = parser.parse(src)
    incidents = [n for n in result.nodes if n.type == NodeType.INCIDENT]
    assert incidents
    inc = incidents[0]
    assert "rx-order-service" in inc.attributes["affected_services"]
    assert inc.attributes["severity"].startswith("SEV")
    assert result.chunks  # section chunks emitted


def test_readme_chunks_go_to_docs_or_architecture(settings, fixture_repo):
    parser = MarkdownParser(settings, ContentType.DOCS)
    src = _source(fixture_repo, "README.md", ContentType.DOCS)
    result = parser.parse(src)
    collections = {c.collection for c in result.chunks}
    assert collections & {"docs", "architecture"}
