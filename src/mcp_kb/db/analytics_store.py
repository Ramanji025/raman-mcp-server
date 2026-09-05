"""P4.2: Query analytics — logs every tool call to Postgres (or JSONL fallback).

Schema (auto-created):
  CREATE TABLE IF NOT EXISTS query_analytics (
      id          BIGSERIAL PRIMARY KEY,
      ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
      tool        TEXT NOT NULL,
      intent      TEXT,
      handler     TEXT,
      query_text  TEXT,
      latency_ms  INTEGER,
      chunks_used INTEGER,
      llm_called  BOOLEAN,
      rating      SMALLINT,       -- filled in later by rate_answer
      interaction_id TEXT
  );
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..config import Settings
from ..logging import get_logger

log = get_logger(__name__)


@dataclass
class QueryRecord:
    """One recorded `ask`/tool-call interaction for query analytics."""

    tool: str
    intent: str = ""
    handler: str = ""
    query_text: str = ""
    latency_ms: int = 0
    chunks_used: int = 0
    llm_called: bool = False
    interaction_id: str = ""


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS query_analytics (
    id             BIGSERIAL PRIMARY KEY,
    ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
    tool           TEXT NOT NULL,
    intent         TEXT,
    handler        TEXT,
    query_text     TEXT,
    latency_ms     INTEGER,
    chunks_used    INTEGER,
    llm_called     BOOLEAN,
    rating         SMALLINT,
    interaction_id TEXT
)
"""

_INSERT = """
INSERT INTO query_analytics (tool, intent, handler, query_text, latency_ms,
                              chunks_used, llm_called, interaction_id)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
"""

_UPDATE_RATING = """
UPDATE query_analytics SET rating = %s
WHERE interaction_id = %s AND rating IS NULL
"""


class QueryAnalyticsStore:
    """Thread-safe analytics store; falls back to JSONL when Postgres is unavailable."""

    def __init__(self, settings: Settings) -> None:
        self._lock = threading.Lock()
        self._conn = None
        self._jsonl: Path | None = None
        if settings.postgres_enabled:
            try:
                import psycopg2
                self._conn = psycopg2.connect(settings.postgres_dsn)
                self._conn.autocommit = True
                with self._conn.cursor() as cur:
                    cur.execute(_CREATE_TABLE)
                log.info("query_analytics_backend", backend="postgres")
            except Exception as exc:
                log.warning("query_analytics_postgres_unavailable", error=str(exc))
                self._conn = None
        if self._conn is None:
            self._jsonl = Path(settings.repos_root).parent / "query_analytics.jsonl"
            log.info("query_analytics_backend", backend="jsonl", path=str(self._jsonl))

    def record(self, rec: QueryRecord) -> None:
        """Append one query analytics record (Postgres if available, else JSONL)."""
        try:
            with self._lock:
                if self._conn is not None:
                    with self._conn.cursor() as cur:
                        cur.execute(_INSERT, (
                            rec.tool, rec.intent, rec.handler, rec.query_text[:512],
                            rec.latency_ms, rec.chunks_used, rec.llm_called,
                            rec.interaction_id,
                        ))
                elif self._jsonl is not None:
                    row = {**asdict(rec), "ts": datetime.now(UTC).isoformat()}
                    with self._jsonl.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(row) + "\n")
        except Exception as exc:
            log.warning("query_analytics_record_failed", error=str(exc))

    def record_rating(self, interaction_id: str, rating: int) -> None:
        """Update the rating for a previously-recorded interaction (Postgres backend only)."""
        if not interaction_id:
            return
        try:
            with self._lock:
                if self._conn is not None:
                    with self._conn.cursor() as cur:
                        cur.execute(_UPDATE_RATING, (rating, interaction_id))
        except Exception as exc:
            log.warning("query_analytics_rating_failed", error=str(exc))

    def close(self) -> None:
        """Close the Postgres connection, if one is open."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass


# Module-level singleton; created lazily on first use.
_store: QueryAnalyticsStore | None = None
_store_lock = threading.Lock()


def get_analytics_store(settings: Settings) -> QueryAnalyticsStore:
    """Return the process-wide QueryAnalyticsStore singleton, creating it on first use."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = QueryAnalyticsStore(settings)
    return _store
