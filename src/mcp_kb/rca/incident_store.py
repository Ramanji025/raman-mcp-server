"""First-class incident persistence — the unit of RCA "knowledge memory".

Incidents are persisted three ways so every retrieval path in the platform
can find them:

1. **Structured record** (Postgres table ``incidents``, or a local JSON
   fallback) — the source of truth for status/severity/fix workflows.
2. **Knowledge-graph node** (``NodeType.INCIDENT``) with ``AFFECTS`` edges to
   the services it touched — so ``impact_analysis``/graph traversal surfaces
   past incidents as blast-radius context.
3. **Vector chunk** in the ``incidents`` Qdrant collection — so future
   ``analyse_exception``/``find_related_incidents`` calls retrieve it via the
   existing hybrid retriever, no separate similarity engine required.

Every write is best-effort per backend: a missing Qdrant/Postgres/graph does
not fail the RCA request, it just narrows what's searchable next time
(logged at WARNING).
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from ..config import Settings
from ..graph.base import GraphStorePort
from ..logging import get_logger
from ..models import (
    Chunk,
    ContentType,
    EdgeType,
    GraphEdge,
    GraphNode,
    IncidentRecord,
    NodeType,
    Severity,
)

log = get_logger(__name__)


class IncidentStore:
    """Facade persisting incidents to Postgres/JSON + graph + vector store."""

    def __init__(
        self,
        settings: Settings,
        graph: GraphStorePort | None = None,
        indexer: Any | None = None,   # VectorIndexer, imported lazily by callers
        retriever: Any | None = None,  # HybridRetriever, imported lazily by callers
    ) -> None:
        self._settings = settings
        self._graph = graph
        self._indexer = indexer
        self._retriever = retriever
        self._backend: _Backend
        if settings.postgres_enabled:
            try:
                self._backend = _PostgresBackend(settings)
                log.info("incident_backend", backend="postgres")
            except Exception as exc:
                log.warning("incident_postgres_unavailable", error=str(exc))
                self._backend = _JsonBackend(settings)
        else:
            self._backend = _JsonBackend(settings)

    # ------------------------------------------------------------------ #
    def save(self, incident: IncidentRecord) -> IncidentRecord:
        """Persist an incident and mirror it into the graph + vector index."""
        if not incident.id:
            incident.id = str(uuid.uuid4())
        self._backend.save(incident)
        self._mirror_to_graph(incident)
        self._mirror_to_vector(incident)
        log.info("incident_saved", id=incident.id, severity=incident.severity.value,
                  service=incident.service_name)
        return incident

    def get(self, incident_id: str) -> IncidentRecord | None:
        """Return the incident with `incident_id`, or None if not found."""
        return self._backend.get(incident_id)

    def list(self, *, service: str | None = None, severity: Severity | None = None,
             status: str | None = None, limit: int = 50) -> list[IncidentRecord]:
        """List incidents, optionally filtered by service/severity/status."""
        return self._backend.list(service=service, severity=severity, status=status,
                                   limit=limit)

    def find_similar(self, query: str, *, top_k: int = 5) -> list[dict[str, Any]]:
        """Semantic search over previously-indexed incidents (best-effort)."""
        if self._retriever is None:
            return self._keyword_fallback(query, top_k)
        try:
            result = self._retriever.retrieve(query, collections=("incidents",), top_k=top_k)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("incident_vector_search_failed", error=str(exc))
            return self._keyword_fallback(query, top_k)
        out = []
        for rc in result.chunks:
            meta = rc.chunk.metadata
            out.append({
                "incident_id": meta.get("incident_id"),
                "title": meta.get("title", rc.chunk.text.splitlines()[0][:120]),
                "severity": meta.get("severity"),
                "service": meta.get("service"),
                "score": round(rc.score, 4),
                "excerpt": rc.chunk.text.strip()[:300],
            })
        return out

    def _keyword_fallback(self, query: str, top_k: int) -> list[dict[str, Any]]:
        """Zero-dependency similarity when no vector retriever is wired up."""
        needle_terms = {t.lower() for t in query.split() if len(t) > 2}
        scored: list[tuple[int, IncidentRecord]] = []
        for incident in self._backend.list(limit=500):
            haystack = f"{incident.title} {incident.description} {incident.exception_type or ''}".lower()
            score = sum(1 for t in needle_terms if t in haystack)
            if score:
                scored.append((score, incident))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [{
            "incident_id": inc.id, "title": inc.title, "severity": inc.severity.value,
            "service": inc.service_name, "score": float(s), "excerpt": inc.description[:300],
        } for s, inc in scored[:top_k]]

    # ------------------------------------------------------------------ #
    def _mirror_to_graph(self, incident: IncidentRecord) -> None:
        if self._graph is None:
            return
        try:
            node = GraphNode(
                id=f"incident:{incident.id}", type=NodeType.INCIDENT, name=incident.title,
                service=incident.service_name,
                attributes={
                    "severity": incident.severity.value, "status": incident.status,
                    "exception_type": incident.exception_type, "confidence": incident.confidence,
                    "environment": incident.environment,
                    "version": incident.version,
                    "exception_class": incident.exception_class,
                    "exception_method": incident.exception_method,
                    "created_at": incident.created_at.isoformat(),
                },
            )
            self._graph.add_node(node)
            for svc in incident.affected_services or ([incident.service_name]
                                                       if incident.service_name else []):
                svc_nodes = self._graph.find_nodes(svc, {NodeType.SERVICE}, limit=1)
                target_id = svc_nodes[0]["id"] if svc_nodes else f"service:{svc}:{svc}"
                self._graph.add_edge(GraphEdge(src=node.id, dst=target_id, type=EdgeType.AFFECTS))

            # Link incident directly to implicated code entities when available.
            for area in incident.metadata.get("related_code_areas", []):
                node_id = area.get("node_id")
                if not node_id:
                    continue
                self._graph.add_edge(
                    GraphEdge(src=node.id, dst=node_id, type=EdgeType.USES,
                              attributes={"role": "related_code_area"})
                )

                # If this is a class/method, also link neighboring endpoints to aid RCA navigation.
                for nb in self._graph.neighbors(node_id, direction="in")[:30]:
                    n = nb.get("node") or {}
                    if n.get("type") == NodeType.ENDPOINT.value:
                        self._graph.add_edge(
                            GraphEdge(src=node.id, dst=n["id"], type=EdgeType.USES,
                                      attributes={"role": "related_endpoint"})
                        )
            self._graph.save()
        except Exception as exc:  # pragma: no cover - never fail RCA on graph mirroring
            log.warning("incident_graph_mirror_failed", error=str(exc))

    def _mirror_to_vector(self, incident: IncidentRecord) -> None:
        if self._indexer is None:
            return
        try:
            text = "\n".join(filter(None, [
                incident.title, incident.description,
                f"Exception: {incident.exception_type}" if incident.exception_type else None,
                f"Root cause: {incident.root_cause}" if incident.root_cause else None,
                f"Fix: {incident.fix_summary}" if incident.fix_summary else None,
            ]))
            chunk = Chunk(
                id=f"incident:{incident.id}", repo=incident.service_name or "platform",
                rel_path=f"incidents/{incident.id}.md", content_type=ContentType.INCIDENTS,
                collection="incidents", text=text,
                metadata={
                    "incident_id": incident.id, "title": incident.title,
                    "severity": incident.severity.value, "service": incident.service_name,
                    "root_cause": incident.root_cause or "",
                    "fix_summary": incident.fix_summary or "",
                    "environment": incident.environment or "",
                    "version": incident.version or "",
                    "exception_type": incident.exception_type or "",
                    "kind": "incident",
                },
            )
            self._indexer.index([chunk])
        except Exception as exc:  # pragma: no cover
            log.warning("incident_vector_mirror_failed", error=str(exc))


# --------------------------------------------------------------------------- #
# Structured-record backends (mirrors db/metadata_store.py's pattern)
# --------------------------------------------------------------------------- #
class _Backend:
    """Storage backend contract for IncidentStore (JSON or Postgres implementation)."""

    def save(self, incident: IncidentRecord) -> None:
        """Persist an incident record."""
    def get(self, incident_id: str) -> IncidentRecord | None:
        """Return the incident with `incident_id`, or None if not found."""
    def list(self, *, service=None, severity=None, status=None, limit=50) -> list[IncidentRecord]:
        """List incidents, optionally filtered by service/severity/status."""


class _JsonBackend(_Backend):
    def __init__(self, settings: Settings) -> None:
        self._path = Path(settings.repos_root).parent / "incidents.json"
        self._state: dict[str, Any] = {"incidents": {}}
        if self._path.exists():
            self._state = json.loads(self._path.read_text(encoding="utf-8"))

    def save(self, incident: IncidentRecord) -> None:
        """Persist an incident record to the JSON store."""
        self._state["incidents"][incident.id] = incident.model_dump(mode="json")
        self._flush()

    def get(self, incident_id: str) -> IncidentRecord | None:
        """Return the incident with `incident_id`, or None if not found."""
        raw = self._state["incidents"].get(incident_id)
        return IncidentRecord.model_validate(raw) if raw else None

    def list(self, *, service=None, severity=None, status=None, limit=50) -> list[IncidentRecord]:
        """List incidents, optionally filtered by service/severity/status."""
        out = []
        for raw in self._state["incidents"].values():
            rec = IncidentRecord.model_validate(raw)
            if service and rec.service_name != service:
                continue
            if severity and rec.severity != severity:
                continue
            if status and rec.status != status:
                continue
            out.append(rec)
        out.sort(key=lambda r: r.created_at, reverse=True)
        return out[:limit]

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._state, indent=2), encoding="utf-8")


class _PostgresBackend(_Backend):
    def __init__(self, settings: Settings) -> None:
        import psycopg

        self._conn = psycopg.connect(settings.postgres_dsn, autocommit=True)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS incidents (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    severity TEXT NOT NULL DEFAULT 'P3',
                    status TEXT NOT NULL DEFAULT 'open',
                    service_name TEXT,
                    environment TEXT,
                    version TEXT,
                    exception_type TEXT,
                    exception_class TEXT,
                    exception_method TEXT,
                    stack_trace TEXT,
                    logs TEXT,
                    root_cause TEXT,
                    rca_document TEXT,
                    confidence DOUBLE PRECISION DEFAULT 0,
                    fix_summary TEXT,
                    resolution TEXT,
                    related_commit TEXT,
                    affected_services JSONB DEFAULT '[]'::jsonb,
                    metadata JSONB DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    resolved_at TIMESTAMPTZ
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_incidents_service ON incidents(service_name)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_incidents_severity ON incidents(severity)")

    def save(self, incident: IncidentRecord) -> None:
        """Persist an incident record to Postgres (upsert by id)."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO incidents (id, title, description, severity, status, service_name,
                    environment, version, exception_type, exception_class, exception_method,
                    stack_trace, logs, root_cause, rca_document, confidence, fix_summary,
                    resolution,
                    related_commit, affected_services, metadata, created_at, resolved_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET
                    title = EXCLUDED.title, description = EXCLUDED.description,
                    severity = EXCLUDED.severity, status = EXCLUDED.status,
                    environment = EXCLUDED.environment, version = EXCLUDED.version,
                    exception_class = EXCLUDED.exception_class,
                    exception_method = EXCLUDED.exception_method,
                    root_cause = EXCLUDED.root_cause, confidence = EXCLUDED.confidence,
                    rca_document = EXCLUDED.rca_document,
                    fix_summary = EXCLUDED.fix_summary, resolution = EXCLUDED.resolution,
                    resolved_at = EXCLUDED.resolved_at
                """,
                (incident.id, incident.title, incident.description, incident.severity.value,
                 incident.status, incident.service_name, incident.environment,
                 incident.version, incident.exception_type, incident.exception_class,
                 incident.exception_method, incident.stack_trace, incident.logs,
                 incident.root_cause, incident.rca_document, incident.confidence,
                 incident.fix_summary, incident.resolution, incident.related_commit,
                 json.dumps(incident.affected_services), json.dumps(incident.metadata),
                 incident.created_at, incident.resolved_at),
            )

    def get(self, incident_id: str) -> IncidentRecord | None:
        """Return the incident with `incident_id`, or None if not found."""
        with self._conn.cursor() as cur:
            cur.execute("SELECT * FROM incidents WHERE id = %s", (incident_id,))
            row = cur.fetchone()
            if not row:
                return None
            cols = [d.name for d in cur.description]
            return self._row_to_record(dict(zip(cols, row)))

    def list(self, *, service=None, severity=None, status=None, limit=50) -> list[IncidentRecord]:
        """List incidents, optionally filtered by service/severity/status."""
        clauses, params = [], []
        if service:
            clauses.append("service_name = %s")
            params.append(service)
        if severity:
            clauses.append("severity = %s")
            params.append(severity.value)
        if status:
            clauses.append("status = %s")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._conn.cursor() as cur:
            # `clauses` only ever contains the fixed literal strings above
            # ("service_name = %s", etc.) — never user input — and all actual
            # values are passed through the parameterized %s placeholders in
            # `params`/`limit`, so this is not an injection vector.
            cur.execute(
                f"SELECT * FROM incidents {where} ORDER BY created_at DESC LIMIT %s",  # nosec B608
                (*params, limit),
            )
            cols = [d.name for d in cur.description]
            return [self._row_to_record(dict(zip(cols, row))) for row in cur.fetchall()]

    @staticmethod
    def _row_to_record(row: dict) -> IncidentRecord:
        return IncidentRecord(
            id=row["id"], title=row["title"], description=row.get("description") or "",
            severity=Severity(row["severity"]), status=row["status"],
            service_name=row.get("service_name"), environment=row.get("environment"),
            version=row.get("version"), exception_type=row.get("exception_type"),
            exception_class=row.get("exception_class"), exception_method=row.get("exception_method"),
            stack_trace=row.get("stack_trace"), logs=row.get("logs"),
            root_cause=row.get("root_cause"), rca_document=row.get("rca_document"),
            confidence=row.get("confidence") or 0.0, fix_summary=row.get("fix_summary"),
            resolution=row.get("resolution"),
            related_commit=row.get("related_commit"),
            affected_services=row.get("affected_services") or [],
            metadata=row.get("metadata") or {},
            created_at=row["created_at"], resolved_at=row.get("resolved_at"),
        )
