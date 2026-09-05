"""Generate semantic summaries for retrieval-first indexing."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..config import Settings
from ..models import Chunk, ContentType, EdgeType, NodeType, ParseResult


def _fingerprint(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class SemanticSummaryCache:
    """File-backed cache for semantic summaries keyed by summary id."""

    def __init__(self, settings: Settings) -> None:
        self._path = Path(settings.repos_root).parent / "semantic" / "summary_cache.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._state: dict[str, dict[str, str]] = {"entries": {}}
        if self._path.exists():
            try:
                self._state = json.loads(self._path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self._state = {"entries": {}}

    def get(self, key: str, fingerprint: str) -> str | None:
        """Return the cached summary for `key` if its fingerprint still matches, else None."""
        entry = self._state["entries"].get(key)
        if not entry:
            return None
        if entry.get("fingerprint") != fingerprint:
            return None
        return entry.get("summary")

    def set(self, key: str, fingerprint: str, summary: str) -> None:
        """Cache a summary under `key`, tagged with its content fingerprint."""
        self._state["entries"][key] = {"fingerprint": fingerprint, "summary": summary}

    def flush(self) -> None:
        """Persist the cache to disk."""
        self._path.write_text(json.dumps(self._state, indent=2), encoding="utf-8")


class SemanticSummaryBuilder:
    """Build semantic summary chunks for class/method/api/repository nodes."""

    def __init__(self, settings: Settings, cache: SemanticSummaryCache) -> None:
        self._settings = settings
        self._cache = cache

    def build(self, parsed: ParseResult) -> list[Chunk]:
        """Build cached semantic-summary chunks for class/method/api/repository nodes in `parsed`."""
        node_by_id = {n.id: n for n in parsed.nodes}
        in_edges: dict[str, list] = {}
        out_edges: dict[str, list] = {}
        for e in parsed.edges:
            out_edges.setdefault(e.src, []).append(e)
            in_edges.setdefault(e.dst, []).append(e)

        chunks: list[Chunk] = []
        for node in parsed.nodes:
            if node.type not in {
                NodeType.CLASS,
                NodeType.CONTROLLER,
                NodeType.SERVICE_LAYER,
                NodeType.METHOD,
                NodeType.ENDPOINT,
                NodeType.REPOSITORY,
            }:
                continue

            attrs = node.attributes or {}
            source_file = str(attrs.get("file", ""))
            if not source_file:
                continue

            summary_id = f"semantic:{node.type.value}:{node.id}"
            payload = {
                "type": node.type.value,
                "name": node.name,
                "service": node.service,
                "attrs": attrs,
                "in": [(e.src, e.type.value) for e in in_edges.get(node.id, [])],
                "out": [(e.dst, e.type.value) for e in out_edges.get(node.id, [])],
            }
            fp = _fingerprint(payload)
            summary_text = self._cache.get(summary_id, fp)
            if summary_text is None:
                summary_text = self._compose_summary(node, attrs, in_edges.get(node.id, []), out_edges.get(node.id, []), node_by_id)
                self._cache.set(summary_id, fp, summary_text)

            chunks.append(
                Chunk(
                    id=summary_id,
                    repo=node.service or "platform",
                    rel_path=source_file,
                    content_type=ContentType.SEMANTIC,
                    collection="semantic",
                    text=summary_text,
                    start_line=attrs.get("start_line"),
                    end_line=attrs.get("end_line"),
                    metadata={
                        "service": node.service or "",
                        "kind": "semantic_summary",
                        "node_type": node.type.value,
                        "node_id": node.id,
                        "symbol": node.name,
                    },
                )
            )
        self._cache.flush()
        return chunks

    def _compose_summary(self, node, attrs: dict[str, Any], in_edges, out_edges, node_by_id: dict[str, Any]) -> str:
        dependencies = self._related_names(out_edges, node_by_id, {EdgeType.CALLS, EdgeType.DEPENDS_ON, EdgeType.DELEGATES_TO, EdgeType.USES, EdgeType.USES_DEPENDENCY})
        consumers = self._related_names(in_edges, node_by_id, {EdgeType.CALLS, EdgeType.DEPENDS_ON, EdgeType.DELEGATES_TO, EdgeType.USES})
        producers = self._related_names(out_edges, node_by_id, {EdgeType.PUBLISHES, EdgeType.PRODUCES_TO})

        failure_modes = self._infer_failure_modes(node.type, attrs)
        responsibilities = self._infer_responsibilities(node.type, attrs)
        purpose = self._infer_purpose(node.type, node.name, attrs)
        business_meaning = self._infer_business_meaning(node.type, node.name, attrs)

        lines = [
            f"Semantic Summary: {node.type.value} {node.name}",
            f"Service: {node.service or 'unknown'}",
            f"Purpose: {purpose}",
            f"Responsibilities: {responsibilities}",
            f"Dependencies: {', '.join(dependencies) if dependencies else 'none'}",
            f"Failure Modes: {failure_modes}",
            f"Business Meaning: {business_meaning}",
            f"Consumers: {', '.join(consumers) if consumers else 'none'}",
            f"Producers: {', '.join(producers) if producers else 'none'}",
        ]

        if node.type == NodeType.ENDPOINT:
            lines.append(f"API: {(attrs.get('http_method') or 'HTTP')} {attrs.get('path') or node.name}")
        if node.type == NodeType.METHOD:
            lines.append(f"Signature: {attrs.get('class', '?')}.{node.name} -> {attrs.get('returns', 'void')}")
        if node.type == NodeType.REPOSITORY:
            qms = attrs.get("query_methods", [])
            lines.append(f"Repository Methods: {', '.join(m.get('name', '?') for m in qms[:8]) if qms else 'none'}")

        return "\n".join(lines)

    @staticmethod
    def _related_names(edges, node_by_id: dict[str, Any], edge_types: set[EdgeType]) -> list[str]:
        names: list[str] = []
        for e in edges:
            if e.type not in edge_types:
                continue
            other_id = e.dst
            if other_id in node_by_id:
                names.append(node_by_id[other_id].name)
            else:
                names.append(other_id.split(":")[-1])
        # preserve order while deduping
        out = []
        seen = set()
        for n in names:
            if n in seen:
                continue
            seen.add(n)
            out.append(n)
        return out[:20]

    @staticmethod
    def _infer_failure_modes(node_type: NodeType, attrs: dict[str, Any]) -> str:
        thrown = attrs.get("thrown_exceptions", [])
        if thrown:
            return "Exceptions: " + ", ".join(str(x) for x in thrown[:8])
        if node_type == NodeType.REPOSITORY:
            return "Database connectivity/query failure, transaction rollback"
        if node_type == NodeType.ENDPOINT:
            return "Input validation failure, downstream dependency timeout"
        if node_type == NodeType.METHOD:
            return "Business rule violation, null/invalid state handling"
        return "Dependency unavailable, invalid input, runtime exception"

    @staticmethod
    def _infer_responsibilities(node_type: NodeType, attrs: dict[str, Any]) -> str:
        if node_type == NodeType.ENDPOINT:
            return "Receives API requests, validates inputs, delegates to services"
        if node_type == NodeType.REPOSITORY:
            return "Persists and queries domain data"
        if node_type == NodeType.METHOD:
            called = attrs.get("called_methods", [])
            return f"Executes business logic and orchestrates {len(called)} downstream call(s)"
        if node_type in {NodeType.SERVICE_LAYER, NodeType.CLASS, NodeType.CONTROLLER}:
            return "Coordinates domain behavior and integration interactions"
        return "Implements domain capability"

    @staticmethod
    def _infer_purpose(node_type: NodeType, name: str, attrs: dict[str, Any]) -> str:
        lname = name.lower()
        if node_type == NodeType.ENDPOINT:
            return f"Expose API operation {(attrs.get('http_method') or 'HTTP')} {attrs.get('path') or name}"
        if node_type == NodeType.METHOD:
            if lname.startswith(("get", "find", "fetch", "list")):
                return "Retrieve data for upstream workflows"
            if lname.startswith(("create", "save", "submit", "place")):
                return "Create/persist business transaction data"
            if lname.startswith(("update", "modify", "patch")):
                return "Update existing business state"
            if lname.startswith(("delete", "remove", "cancel")):
                return "Delete/deactivate business state"
            return "Execute business operation"
        if node_type == NodeType.REPOSITORY:
            return "Provide database access for aggregate lifecycle"
        return "Provide business capability implementation"

    @staticmethod
    def _infer_business_meaning(node_type: NodeType, name: str, attrs: dict[str, Any]) -> str:
        path = str(attrs.get("path", ""))
        if "checkout" in (name.lower() + path.lower()):
            return "Part of checkout capability"
        if "payment" in (name.lower() + path.lower()):
            return "Part of payment processing capability"
        if "inventory" in (name.lower() + path.lower()):
            return "Part of inventory management capability"
        if "order" in (name.lower() + path.lower()):
            return "Part of order processing capability"
        if node_type == NodeType.REPOSITORY:
            return "Persists domain records for business continuity"
        return "Supports core domain workflow"
