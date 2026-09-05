"""Metadata store abstraction.

Tracks per-file content hashes so re-ingestion can skip unchanged files and
rebuild embeddings only for what changed (goal #8). Uses Postgres when
``POSTGRES_ENABLED`` is set; otherwise falls back to a local JSON file so the
whole system still runs with zero external services.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import Settings
from ..logging import get_logger

log = get_logger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class MetadataStore:
    """Facade choosing a Postgres or JSON backend at construction time."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._backend: _Backend
        if settings.postgres_enabled:
            try:
                self._backend = _PostgresBackend(settings)
                log.info("metadata_backend", backend="postgres")
            except Exception as exc:  # graceful local fallback
                log.warning("postgres_unavailable", error=str(exc))
                self._backend = _JsonBackend(settings)
        else:
            self._backend = _JsonBackend(settings)
            log.info("metadata_backend", backend="json")

    # ---- change detection ---- #
    def known_hashes(self, repo: str) -> dict[str, str]:
        """Map of ``rel_path -> sha256`` last indexed for ``repo``."""
        return self._backend.known_hashes(repo)

    def record_file(self, repo: str, rel_path: str, content_type: str,
                    sha256: str, chunk_count: int) -> None:
        """Record/update the last-indexed hash + chunk count for one repo file."""
        self._backend.record_file(repo, rel_path, content_type, sha256, chunk_count)

    def forget_files(self, repo: str, rel_paths: list[str]) -> None:
        """Remove indexing state for files that no longer exist (e.g. deleted from repo)."""
        self._backend.forget_files(repo, rel_paths)

    def set_repo_commit(self, repo: str, commit: str) -> None:
        """Record the git commit hash last successfully ingested for a repo."""
        self._backend.set_repo_commit(repo, commit)

    def get_repo_commit(self, repo: str) -> str | None:
        """Return the last-ingested git commit hash for a repo, or None if never indexed."""
        return self._backend.get_repo_commit(repo)

    def get_repo_last_indexed(self, repo: str) -> str | None:
        """ISO timestamp of the last successful ingest for ``repo``, or None if never indexed."""
        return self._backend.get_repo_last_indexed(repo)

    def close(self) -> None:
        """Close/flush the underlying backend."""
        self._backend.close()


class _Backend:
    """Storage backend contract for MetadataStore (JSON or Postgres implementation)."""

    def known_hashes(self, repo: str) -> dict[str, str]:
        """Return {rel_path: sha256} for every file last indexed for `repo`."""
    def record_file(self, repo, rel_path, content_type, sha256, chunk_count):
        """Record/update the last-indexed hash + chunk count for one repo file."""
    def forget_files(self, repo: str, rel_paths: list[str]):
        """Remove indexing state for the given files."""
    def set_repo_commit(self, repo: str, commit: str):
        """Record the git commit hash last successfully ingested for a repo."""
    def get_repo_commit(self, repo: str) -> str | None:
        """Return the last-ingested git commit hash for a repo, or None if never indexed."""
    def get_repo_last_indexed(self, repo: str) -> str | None:
        """Return the ISO timestamp of the last successful ingest for a repo, or None."""
    def close(self):
        """Close/flush the backend."""


class _JsonBackend(_Backend):
    """Zero-dependency fallback storing state under ``data/metadata.json``."""

    def __init__(self, settings: Settings) -> None:
        self._path = Path(settings.repos_root).parent / "metadata.json"
        self._state: dict[str, Any] = {"repos": {}}
        if self._path.exists():
            self._state = json.loads(self._path.read_text(encoding="utf-8"))

    def _repo(self, repo: str) -> dict:
        return self._state["repos"].setdefault(repo, {"commit": None, "files": {}})

    def known_hashes(self, repo: str) -> dict[str, str]:
        """Return {rel_path: sha256} for every file last indexed for `repo`."""
        return {p: e["sha256"] for p, e in self._repo(repo)["files"].items()}

    def record_file(self, repo, rel_path, content_type, sha256, chunk_count):
        """Record/update the last-indexed hash + chunk count for one repo file."""
        self._repo(repo)["files"][rel_path] = {
            "content_type": content_type, "sha256": sha256, "chunks": chunk_count}
        self._flush()

    def forget_files(self, repo: str, rel_paths: list[str]):
        """Remove indexing state for the given files."""
        files = self._repo(repo)["files"]
        for p in rel_paths:
            files.pop(p, None)
        self._flush()

    def set_repo_commit(self, repo: str, commit: str):
        """Record the git commit hash last successfully ingested for a repo."""
        self._repo(repo)["commit"] = commit
        self._repo(repo)["last_indexed_at"] = _now_iso()
        self._flush()

    def get_repo_commit(self, repo: str) -> str | None:
        """Return the last-ingested git commit hash for a repo, or None if never indexed."""
        return self._repo(repo)["commit"]

    def get_repo_last_indexed(self, repo: str) -> str | None:
        """Return the ISO timestamp of the last successful ingest for a repo, or None."""
        return self._repo(repo).get("last_indexed_at")

    def _flush(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._state, indent=2), encoding="utf-8")

    def close(self):
        """Flush pending state to disk."""
        self._flush()


class _PostgresBackend(_Backend):
    def __init__(self, settings: Settings) -> None:
        import psycopg

        self._conn = psycopg.connect(settings.postgres_dsn, autocommit=True)
        self._ensure_schema()

    def _ensure_schema(self):
        # Schema is normally created by docker-entrypoint-initdb.d; this is a
        # best-effort fallback. psycopg3 rejects multi-statement execute, so run
        # each statement separately and ignore ones that already exist.
        init = Path("scripts/init_db.sql")
        if not init.exists():
            return
        statements = [s.strip() for s in init.read_text(encoding="utf-8").split(";")
                      if s.strip() and not s.strip().startswith("--")]
        for stmt in statements:
            try:
                with self._conn.cursor() as cur:
                    cur.execute(stmt)
            except Exception as exc:  # already exists / permission -> skip
                log.debug("schema_stmt_skipped", error=str(exc))

    def _repo_id(self, repo: str) -> int:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO repositories(name) VALUES (%s) "
                "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id",
                (repo,),
            )
            return cur.fetchone()[0]

    def known_hashes(self, repo: str) -> dict[str, str]:
        """Return {rel_path: sha256} for every file last indexed for `repo`."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT f.rel_path, f.sha256 FROM ingested_files f "
                "JOIN repositories r ON r.id = f.repository_id WHERE r.name = %s",
                (repo,),
            )
            return {row[0]: row[1] for row in cur.fetchall()}

    def record_file(self, repo, rel_path, content_type, sha256, chunk_count):
        """Record/update the last-indexed hash + chunk count for one repo file."""
        rid = self._repo_id(repo)
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ingested_files(repository_id, rel_path, content_type, "
                "sha256, chunk_count) VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT (repository_id, rel_path) DO UPDATE SET "
                "sha256 = EXCLUDED.sha256, content_type = EXCLUDED.content_type, "
                "chunk_count = EXCLUDED.chunk_count, indexed_at = now()",
                (rid, rel_path, content_type, sha256, chunk_count),
            )

    def forget_files(self, repo: str, rel_paths: list[str]):
        """Remove indexing state for the given files."""
        if not rel_paths:
            return
        rid = self._repo_id(repo)
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM ingested_files WHERE repository_id = %s AND rel_path = ANY(%s)",
                (rid, rel_paths),
            )

    def set_repo_commit(self, repo: str, commit: str):
        """Record the git commit hash last successfully ingested for a repo."""
        rid = self._repo_id(repo)
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE repositories SET last_commit = %s, last_indexed = now() WHERE id = %s",
                (commit, rid),
            )

    def get_repo_commit(self, repo: str) -> str | None:
        """Return the last-ingested git commit hash for a repo, or None if never indexed."""
        with self._conn.cursor() as cur:
            cur.execute("SELECT last_commit FROM repositories WHERE name = %s", (repo,))
            row = cur.fetchone()
            return row[0] if row else None

    def get_repo_last_indexed(self, repo: str) -> str | None:
        """Return the ISO timestamp of the last successful ingest for a repo, or None."""
        with self._conn.cursor() as cur:
            cur.execute("SELECT last_indexed FROM repositories WHERE name = %s", (repo,))
            row = cur.fetchone()
            return row[0].isoformat() if row and row[0] else None

    def close(self):
        """Close the Postgres connection."""
        self._conn.close()
