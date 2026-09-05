"""Markdown/AsciiDoc parser for docs, architecture notes and incident reports."""
from __future__ import annotations

import re

from ...config import Settings
from ...models import ContentType, GraphNode, NodeType, ParseResult, SourceFile
from .base import Parser, make_node_id


class MarkdownParser(Parser):
    """Heading-aware chunking; incident docs also become ``Incident`` nodes."""

    def __init__(self, settings: Settings, content_type: ContentType) -> None:
        super().__init__(settings)
        self._content_type = content_type

    def parse(self, source: SourceFile) -> ParseResult:
        """Parse one Markdown file into docs/incidents chunks."""
        result = ParseResult()
        service = self._service_name(source)
        text = self._read(source)
        title = self._title(text) or source.rel_path

        collection = "incidents" if self._content_type == ContentType.INCIDENTS else "docs"
        if self._is_architecture(source.rel_path, title):
            collection = "architecture"

        if self._content_type == ContentType.INCIDENTS:
            inc_id = make_node_id("incident", service, source.rel_path)
            result.nodes.append(
                GraphNode(
                    id=inc_id, type=NodeType.INCIDENT, name=title, service=service,
                    attributes={
                        "file": source.rel_path,
                        "affected_services": self._mentioned_services(text),
                        "severity": self._severity(text),
                    },
                )
            )

        for ordinal, (heading, body) in enumerate(self._sections(text)):
            piece = f"# {heading}\n{body}".strip()
            if not piece:
                continue
            for chunk in self._chunk_text(
                source, piece, collection=collection,
                metadata={"title": title, "heading": heading, "service": service,
                          "doc_type": self._content_type.value},
            ):
                chunk.id = f"{chunk.id}-{ordinal}"
                result.chunks.append(chunk)
        return result

    # ---- helpers ---- #
    @staticmethod
    def _title(text: str) -> str:
        m = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        return m.group(1).strip() if m else ""

    @staticmethod
    def _sections(text: str) -> list[tuple[str, str]]:
        parts = re.split(r"^(#{1,3})\s+(.+)$", text, flags=re.MULTILINE)
        if len(parts) < 4:
            return [("", text)]
        sections: list[tuple[str, str]] = []
        # parts = [pre, hashes, heading, body, hashes, heading, body, ...]
        i = 1
        if parts[0].strip():
            sections.append(("", parts[0].strip()))
        while i + 2 < len(parts):
            heading = parts[i + 1].strip()
            body = parts[i + 2].strip()
            sections.append((heading, body))
            i += 3
        return sections

    @staticmethod
    def _is_architecture(rel_path: str, title: str) -> bool:
        needle = f"{rel_path} {title}".lower()
        return any(k in needle for k in ("architecture", "design", "adr", "system"))

    @staticmethod
    def _mentioned_services(text: str) -> list[str]:
        return sorted(set(re.findall(r"\b(rx-[a-z0-9\-]+service)\b", text)))

    @staticmethod
    def _severity(text: str) -> str | None:
        m = re.search(r"\b(sev[\s\-]?[0-5]|p[0-4]|critical|high|medium|low)\b",
                      text, re.IGNORECASE)
        return m.group(1).upper() if m else None
