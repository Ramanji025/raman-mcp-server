"""Persistence for structured knowledge objects (JSONL, local-first)."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import ValidationError

from ..config import Settings
from .models import KnowledgeObject

logger = logging.getLogger(__name__)

_CONFLICT_MARKERS = ("<<<<<<<", "=======", ">>>>>>>")


class KnowledgeStore:
    """JSONL-backed persistence for KnowledgeObject records."""

    def __init__(self, settings: Settings) -> None:
        root = Path(settings.repos_root).parent / "knowledge"
        root.mkdir(parents=True, exist_ok=True)
        self._path = root / "objects.jsonl"

    def upsert_many(self, objects: list[KnowledgeObject]) -> int:
        """Insert or update a batch of knowledge objects by id; return the count written."""
        if not objects:
            return 0
        existing = {obj.id: obj for obj in self._read_all()}
        for obj in objects:
            existing[obj.id] = obj
        self._write_all(list(existing.values()))
        return len(objects)

    def delete_by_paths(self, repository: str, rel_paths: list[str]) -> None:
        """Remove knowledge objects previously written for the given repo files."""
        if not rel_paths or not self._path.exists():
            return
        blocked = set(rel_paths)
        kept = [
            obj for obj in self._read_all()
            if not (obj.repository == repository and (obj.file_path or "") in blocked)
        ]
        self._write_all(kept)

    def _read_all(self) -> list[KnowledgeObject]:
        if not self._path.exists():
            return []
        items: list[KnowledgeObject] = []
        with self._path.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                line = line.strip()
                if not line or line.startswith(_CONFLICT_MARKERS):
                    if line.startswith(_CONFLICT_MARKERS):
                        logger.warning(
                            "Skipping git conflict marker at %s:%d", self._path, lineno
                        )
                    continue
                try:
                    items.append(KnowledgeObject.model_validate_json(line))
                except ValidationError:
                    logger.warning(
                        "Skipping invalid knowledge object at %s:%d", self._path, lineno
                    )
        return items

    def _write_all(self, items: list[KnowledgeObject]) -> None:
        with self._path.open("w", encoding="utf-8") as fh:
            for item in items:
                fh.write(json.dumps(item.model_dump(mode="json"), ensure_ascii=True))
                fh.write("\n")
