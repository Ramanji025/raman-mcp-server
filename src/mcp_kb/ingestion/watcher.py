"""Continuous/incremental indexing (Phase 3): background git-poll watcher.

Mirrors codebase-memory-mcp's git-based file-change watcher + daemon
auto-index, but reuses this project's existing git-diff-based incremental
ingestion (`IngestionPipeline.refresh_all`) instead of an OS-level file
watcher — no new dependency, and correctness is identical since ingestion
is already keyed off git commit diffs, not raw filesystem events.

Runs as a single background thread per process (started once by the MCP
server when `WATCHER_ENABLED=true`), polling every `WATCHER_INTERVAL_MINUTES`:
  1. Auto-index any locally-cloned repo never ingested before (bounded by
     `AUTO_INDEX_LIMIT` files) — only when `AUTO_INDEX_ENABLED=true`.
  2. `git pull` + incremental re-index every already-known repo.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from ..config import Settings
from ..logging import get_logger
from .single_instance import SingleInstanceLock

log = get_logger(__name__)


class IndexWatcher:
    """Background thread that keeps the knowledge graph in sync with git."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = SingleInstanceLock(Path(settings.repos_root).parent / "locks", "watcher")

    def start(self) -> None:
        """Start the background polling thread (no-op if already running, and
        no-op if another live process on this machine already owns the
        watcher lock — follow-on gap 4: single-instance admission barrier,
        avoids duplicate git-pull/re-index races across multiple connected
        agent client sessions)."""
        if not self._settings.watcher_enabled:
            log.info("watcher_disabled")
            return
        if self._thread and self._thread.is_alive():
            return
        if not self._lock.acquire():
            log.info("watcher_skipped_another_instance_owns_lock")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="mcp-kb-watcher")
        self._thread.start()
        log.info("watcher_started", interval_minutes=self._settings.watcher_interval_minutes)

    def stop(self) -> None:
        """Signal the background thread to stop and wait briefly for it to exit."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._lock.release()

    def _run(self) -> None:
        interval_s = max(60, self._settings.watcher_interval_minutes * 60)
        # Stagger the first tick so it doesn't race the server's own prewarm ingestion.
        if self._stop_event.wait(timeout=min(30, interval_s)):
            return
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:  # pragma: no cover - background loop must never die
                log.error("watcher_tick_failed", error=str(exc))
            if self._stop_event.wait(timeout=interval_s):
                break

    def _tick(self) -> None:
        from .pipeline import IngestionPipeline

        pipeline = IngestionPipeline(self._settings, generate_embeddings=True)
        try:
            if self._settings.auto_index_enabled:
                self._auto_index_new_repos(pipeline)
            reports = pipeline.refresh_all()
            changed = [r for r in reports if r.nodes or r.edges or r.files_deleted]
            if changed:
                log.info("watcher_refresh_done", repos_changed=len(changed),
                         repos_scanned=len(reports))
        finally:
            pipeline.meta.close()

    def _auto_index_new_repos(self, pipeline) -> None:
        limit = self._settings.auto_index_limit
        for name in pipeline.git.discover_local_repos():
            if pipeline.meta.get_repo_commit(name) is not None:
                continue  # already indexed at least once
            repo_dir = self._settings.repos_root / name
            file_count = sum(1 for p in repo_dir.rglob("*") if p.is_file())
            if file_count > limit:
                log.warning("auto_index_skipped_too_large", repo=name,
                           files=file_count, limit=limit)
                continue
            log.info("auto_index_new_repo", repo=name, files=file_count)
            pipeline.ingest_repo(name, incremental=False)


_WATCHER: IndexWatcher | None = None


def get_watcher(settings: Settings) -> IndexWatcher:
    """Return the process-wide IndexWatcher singleton, creating it on first use."""
    global _WATCHER
    if _WATCHER is None:
        _WATCHER = IndexWatcher(settings)
    return _WATCHER
