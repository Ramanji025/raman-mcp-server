"""Audit logging: append-only record of every MCP tool invocation.

Enabled by default (``AUDIT_LOG_ENABLED=true``) since auditability is a
first-class goal — but writing an audit entry never blocks or fails a tool
call (best-effort, like the incident-store mirroring). Persists to Postgres
(``audit_log`` table, see ``scripts/init_db.sql``) when available, otherwise
falls back to a local JSON-lines file so it still works with zero external
services.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import Settings
from ..logging import get_logger

log = get_logger(__name__)


class AuditLog:
    """Facade choosing a Postgres or JSON-lines backend at construction time."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._backend: _Backend
        if not settings.audit_log_enabled:
            self._backend = _NullBackend()
            return
        if settings.postgres_enabled:
            try:
                self._backend = _PostgresBackend(settings)
            except Exception as exc:
                log.warning("audit_postgres_unavailable", error=str(exc))
                self._backend = _JsonLinesBackend(settings)
        else:
            self._backend = _JsonLinesBackend(settings)

    def record(self, *, principal: str, tool: str, query: dict[str, Any],
               repo_scope: str | None, allowed: bool, denial_reason: str | None = None) -> None:
        """Append one audit entry (tool call, principal, allowed/denied); never raises."""
        try:
            self._backend.record(principal=principal, tool=tool, query=query,
                                 repo_scope=repo_scope, allowed=allowed,
                                 denial_reason=denial_reason)
        except Exception as exc:  # pragma: no cover - audit logging must never break a tool call
            log.warning("audit_log_write_failed", error=str(exc))

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return the most recent audit entries, newest first."""
        return self._backend.recent(limit)


class _Backend:
    """Storage backend contract for AuditLog (null/JSONL/Postgres implementation)."""

    def record(self, **kwargs: Any) -> None:
        """Append one audit entry."""
    def recent(self, limit: int) -> list[dict[str, Any]]:
        """Return the most recent audit entries, newest first."""


class _NullBackend(_Backend):
    """No-op backend used when audit logging is disabled."""

    def record(self, **kwargs: Any) -> None:
        """Discard the audit entry (audit logging disabled)."""
        return

    def recent(self, limit: int) -> list[dict[str, Any]]:
        """Always returns an empty list (audit logging disabled)."""
        return []


class _JsonLinesBackend(_Backend):
    """Append-only JSONL file backend for the audit log."""

    def __init__(self, settings: Settings) -> None:
        self._path = Path(settings.repos_root).parent / "audit_log.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, **kwargs: Any) -> None:
        """Append one audit entry as a JSON line."""
        entry = {**kwargs, "occurred_at": datetime.now(UTC).isoformat()}
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def recent(self, limit: int) -> list[dict[str, Any]]:
        """Return the last `limit` audit entries from the JSONL file."""
        if not self._path.exists():
            return []
        lines = self._path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines[-limit:]]


class _PostgresBackend(_Backend):
    """Postgres table backend for the audit log."""

    def __init__(self, settings: Settings) -> None:
        import psycopg

        self._conn = psycopg.connect(settings.postgres_dsn, autocommit=True)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id BIGSERIAL PRIMARY KEY,
                    principal TEXT NOT NULL DEFAULT 'local',
                    tool TEXT NOT NULL,
                    query JSONB NOT NULL DEFAULT '{}'::jsonb,
                    repo_scope TEXT,
                    allowed BOOLEAN NOT NULL DEFAULT true,
                    denial_reason TEXT,
                    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)

    def record(self, *, principal: str, tool: str, query: dict[str, Any],
               repo_scope: str | None, allowed: bool, denial_reason: str | None) -> None:
        """Insert one audit entry into Postgres."""
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO audit_log (principal, tool, query, repo_scope, allowed, "
                "denial_reason) VALUES (%s,%s,%s,%s,%s,%s)",
                (principal, tool, json.dumps(query), repo_scope, allowed, denial_reason),
            )

    def recent(self, limit: int) -> list[dict[str, Any]]:
        """Return the most recent audit entries from Postgres, newest first."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT principal, tool, query, repo_scope, allowed, denial_reason, "
                "occurred_at FROM audit_log ORDER BY occurred_at DESC LIMIT %s", (limit,))
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


_AUDIT_LOG: AuditLog | None = None


def get_audit_log(settings: Settings) -> AuditLog:
    """Return the process-wide AuditLog singleton, creating it on first use."""
    global _AUDIT_LOG
    if _AUDIT_LOG is None:
        _AUDIT_LOG = AuditLog(settings)
    return _AUDIT_LOG
