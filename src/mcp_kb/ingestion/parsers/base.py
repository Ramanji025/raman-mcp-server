"""Parser base class and shared id / chunking helpers."""
from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from ...config import Settings
from ...models import Chunk, ParseResult, SourceFile

if TYPE_CHECKING:
    from ..repository_intelligence.models import ProjectKnowledgeModel


def make_node_id(node_type: str, service: str, name: str) -> str:
    """Deterministic, human-readable graph node id."""
    return f"{node_type.lower()}:{service}:{name}"


def make_chunk_id(repo: str, rel_path: str, ordinal: int, salt: str = "") -> str:
    """Build a stable, deterministic chunk id from repo/path/ordinal/salt."""
    raw = f"{repo}::{rel_path}::{ordinal}::{salt}"
    # usedforsecurity=False: this hash is a deterministic id, never a security
    # control (no secrets/auth), so SHA1's cryptographic weakness is moot here.
    return hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()


class Parser(ABC):
    """Base class for all content parsers."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # Injected per-repo by ParserRegistry before each parse call.
        self._project_model: ProjectKnowledgeModel | None = None

    def set_project_model(self, model: ProjectKnowledgeModel | None) -> None:
        """Receive the pre-analysed project knowledge for the current repo."""
        self._project_model = model

    @abstractmethod
    def parse(self, source: SourceFile) -> ParseResult:  # pragma: no cover
        """Parse one source file into graph nodes/edges/chunks."""
        ...

    # ---- helpers ---------------------------------------------------------- #
    def _read(self, source: SourceFile) -> str:
        with open(source.abs_path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()

    def _chunk_text(
        self,
        source: SourceFile,
        text: str,
        collection: str,
        *,
        metadata: dict | None = None,
    ) -> list[Chunk]:
        """Token-approximate windowed chunking for documentation-like text."""
        window = self._settings.ingest_doc_chunk_tokens
        overlap = max(0, window // 8)
        words = re.split(r"(\s+)", text)
        # Merge whitespace back so we chunk on word boundaries.
        tokens = [t for t in words if t.strip()]
        chunks: list[Chunk] = []
        step = max(1, window - overlap)
        for ordinal, start in enumerate(range(0, len(tokens), step)):
            piece = " ".join(tokens[start : start + window])
            if not piece.strip():
                continue
            chunks.append(
                Chunk(
                    id=make_chunk_id(source.repo, source.rel_path, ordinal),
                    repo=source.repo,
                    rel_path=source.rel_path,
                    content_type=source.content_type,
                    collection=collection,
                    text=piece,
                    metadata={**(metadata or {})},
                )
            )
        return chunks

    def _service_name(self, source: SourceFile) -> str:
        """The owning microservice is the repository name by convention."""
        return source.repo
