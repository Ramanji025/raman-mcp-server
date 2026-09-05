"""Repository scanning + content-type classification and file hashing."""
from __future__ import annotations

import fnmatch
import hashlib
from collections.abc import Iterator
from pathlib import Path

from ..config import Settings
from ..logging import get_logger
from ..models import ContentType, SourceFile

log = get_logger(__name__)


class RepoScanner:
    """Walks a repository and classifies files by ``ContentType`` via globs."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        static = settings.static.get("ingestion", {})
        self._include: dict[str, list[str]] = static.get("include", {})
        self._exclude: list[str] = static.get("exclude", [])
        self._max_bytes = settings.ingest_max_file_size_kb * 1024

    def classify(self, rel_path: str) -> ContentType | None:
        """Return the content type for a path, or ``None`` if not ingestible."""
        posix = rel_path.replace("\\", "/")
        if any(self._matches(posix, pat) for pat in self._exclude):
            return None
        for ctype, patterns in self._include.items():
            if any(self._matches(posix, pat) for pat in patterns):
                try:
                    return ContentType(ctype)
                except ValueError:
                    continue
        return None

    @staticmethod
    def _matches(posix: str, pattern: str) -> bool:
        # fnmatch's ``*`` spans ``/``, so a ``**/`` prefix fails to match
        # root-level files; also try the pattern with that prefix stripped.
        if fnmatch.fnmatch(posix, pattern):
            return True
        if pattern.startswith("**/") and fnmatch.fnmatch(posix, pattern[3:]):
            return True
        return False

    def scan(self, repo_name: str, repo_dir: Path) -> Iterator[SourceFile]:
        """Yield every ingestible file in ``repo_dir``."""
        for path in repo_dir.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(repo_dir).as_posix()
            ctype = self.classify(rel)
            if ctype is None:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > self._max_bytes:
                log.debug("skip_large_file", repo=repo_name, path=rel, size=size)
                continue
            yield SourceFile(
                repo=repo_name,
                rel_path=rel,
                abs_path=str(path),
                content_type=ctype,
                sha256=self.hash_file(path),
                size_bytes=size,
            )

    def scan_paths(
        self, repo_name: str, repo_dir: Path, rel_paths: list[str]
    ) -> Iterator[SourceFile]:
        """Classify + hash a specific set of paths (incremental indexing)."""
        for rel in rel_paths:
            path = repo_dir / rel
            if not path.is_file():
                continue
            ctype = self.classify(rel)
            if ctype is None:
                continue
            yield SourceFile(
                repo=repo_name,
                rel_path=rel.replace("\\", "/"),
                abs_path=str(path),
                content_type=ctype,
                sha256=self.hash_file(path),
                size_bytes=path.stat().st_size,
            )

    @staticmethod
    def hash_file(path: Path) -> str:
        """Return the sha256 hex digest of a file's contents."""
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for block in iter(lambda: fh.read(65536), b""):
                h.update(block)
        return h.hexdigest()
