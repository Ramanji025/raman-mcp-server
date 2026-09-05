"""Idempotent, validated migration from the NetworkX graph snapshot to Neo4j."""
from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import Settings, get_settings
from ..logging import get_logger
from ..models import EdgeType, NodeType
from .knowledge_graph import KnowledgeGraph

log = get_logger(__name__)

_PROMOTED_ATTRS = ("file", "start_line", "end_line", "class")
_VALID_LABELS = {node_type.value for node_type in NodeType}
_VALID_RELATIONSHIPS = {edge_type.value for edge_type in EdgeType}
_SCHEMA = (
    "CREATE CONSTRAINT kbnode_id IF NOT EXISTS FOR (n:KBNode) REQUIRE n.id IS UNIQUE",
    "CREATE INDEX kbnode_type IF NOT EXISTS FOR (n:KBNode) ON (n.type)",
    "CREATE INDEX kbnode_service IF NOT EXISTS FOR (n:KBNode) ON (n.service)",
    "CREATE INDEX kbnode_file IF NOT EXISTS FOR (n:KBNode) ON (n.file)",
    "CREATE INDEX kbnode_name IF NOT EXISTS FOR (n:KBNode) ON (n.name)",
    "CREATE INDEX kbnode_class_name IF NOT EXISTS FOR (n:KBNode) ON (n.class, n.name)",
    "CREATE INDEX kbrelationship_edge_id IF NOT EXISTS FOR ()-[r]-() ON (r.edge_id)",
)


@dataclass(frozen=True)
class MigrationReport:
    """Outcome of migrating one graph.json snapshot into Neo4j."""

    migration_id: str
    source_path: str
    source_sha256: str
    source_nodes: int
    source_edges: int
    neo4j_nodes: int
    neo4j_edges: int
    nodes_written: int
    edges_written: int
    validation_passed: bool
    started_at: str
    completed_at: str
    dry_run: bool


class GraphJsonToNeo4jMigrator:
    """Migrate ``graph.json`` into Neo4j without modifying the source snapshot.

    Node ids and stable edge ids make reruns safe. The source snapshot is copied
    once per migration id, original attributes are retained as JSON, and source
    / target counts are checked before a successful result is returned.
    """

    def __init__(self, settings: Settings | None = None, *, batch_size: int = 500) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self.settings = settings or get_settings()
        self.batch_size = batch_size
        self.snapshot = Path(self.settings.repos_root).parent / "graph" / "graph.json"
        self.report_dir = self.snapshot.parent / "migrations"

    def migrate(self, *, dry_run: bool = False) -> MigrationReport:
        """Migrate the local graph.json snapshot into Neo4j, validating counts match afterward."""
        started = datetime.now(UTC)
        source_hash = self._checksum(self.snapshot)
        migration_id = f"graph-json-{source_hash[:12]}"
        graph = self._load_source()
        source_nodes = graph.raw.number_of_nodes()
        source_edges = graph.raw.number_of_edges()

        if dry_run:
            return self._report(migration_id, source_hash, source_nodes, source_edges,
                                0, 0, 0, 0, True, started, dry_run=True)

        self._backup_snapshot(migration_id)
        from neo4j import GraphDatabase

        from .neo4j_store import _driver_notification_kwargs

        driver = GraphDatabase.driver(
            self.settings.neo4j_uri,
            auth=self.settings.neo4j_auth,
            max_connection_pool_size=50,
            connection_acquisition_timeout=30,
            max_transaction_retry_time=30,
            **_driver_notification_kwargs(),
        )
        try:
            driver.verify_connectivity()
            with driver.session(database=self.settings.neo4j_database) as session:
                self._ensure_schema(session)
                nodes_written = self._write_nodes(session, graph, migration_id, source_hash)
                edges_written = self._write_edges(session, graph, migration_id, source_hash)
                target_nodes = session.run("MATCH (n:KBNode) RETURN count(n) AS count").single()["count"]
                target_edges = session.run(
                    "MATCH (:KBNode)-[r]->(:KBNode) WHERE r.edge_id IS NOT NULL RETURN count(r) AS count"
                ).single()["count"]
        finally:
            driver.close()

        # Validation requires exact equality on a fresh target. On an existing
        # target, compare this migration's source-snapshot records instead.
        validation_passed = self._validate_migration(
            source_nodes, source_edges, target_nodes, target_edges, migration_id
        )
        report = self._report(migration_id, source_hash, source_nodes, source_edges,
                              target_nodes, target_edges, nodes_written, edges_written,
                              validation_passed, started, dry_run=False)
        self._write_report(report)
        if not validation_passed:
            raise RuntimeError(
                "Neo4j migration validation failed; graph.json remains unchanged. "
                f"Read report: {self.report_dir / (migration_id + '.json')}"
            )
        log.info("graph_json_to_neo4j_complete", **asdict(report))
        return report

    def _load_source(self) -> KnowledgeGraph:
        if not self.snapshot.exists():
            raise FileNotFoundError(f"graph snapshot not found: {self.snapshot}")
        graph = KnowledgeGraph(self.settings)
        if not graph.load():
            raise RuntimeError(f"unable to load graph snapshot: {self.snapshot}")
        return graph

    def _write_nodes(self, session: Any, graph: KnowledgeGraph, migration_id: str, source_hash: str) -> int:
        rows_by_type: dict[str, list[dict[str, Any]]] = {}
        for node_id, node in graph.raw.nodes(data=True):
            node_type = str(node.get("type") or "")
            if node_type not in _VALID_LABELS:
                raise ValueError(f"unsupported source node type: {node_type!r}")
            attrs = node.get("attributes", {})
            rows_by_type.setdefault(node_type, []).append({
                "id": node_id,
                "type": node_type,
                "name": node.get("name"),
                "service": node.get("service"),
                "attributes_json": json.dumps(attrs, sort_keys=True, default=str),
                "source_snapshot": migration_id,
                "source_sha256": source_hash,
                **{key: attrs.get(key) for key in _PROMOTED_ATTRS},
            })

        written = 0
        for node_type, rows in rows_by_type.items():
            query = (
                "UNWIND $rows AS row "
                f"MERGE (node:KBNode:`{node_type}` {{id: row.id}}) "
                "SET node.type = row.type, node.name = row.name, node.service = row.service, "
                "node.attributes_json = row.attributes_json, node.file = row.file, "
                "node.start_line = row.start_line, node.end_line = row.end_line, node.class = row.class, "
                "node.source_snapshot = row.source_snapshot, node.source_sha256 = row.source_sha256"
            )
            for batch in self._chunks(rows):
                session.run(query, rows=batch).consume()
                written += len(batch)
        return written

    def _write_edges(self, session: Any, graph: KnowledgeGraph, migration_id: str, source_hash: str) -> int:
        rows_by_type: dict[str, list[dict[str, Any]]] = {}
        for source, target, key, edge in graph.raw.edges(keys=True, data=True):
            relationship_type = str(key)
            if relationship_type not in _VALID_RELATIONSHIPS:
                raise ValueError(f"unsupported source relationship type: {relationship_type!r}")
            attrs = edge.get("attributes", {})
            canonical = json.dumps(attrs, sort_keys=True, default=str)
            edge_id = self._edge_id(source, target, relationship_type, canonical)
            rows_by_type.setdefault(relationship_type, []).append({
                "source": source,
                "target": target,
                "edge_id": edge_id,
                "attributes_json": canonical,
                "source_snapshot": migration_id,
                "source_sha256": source_hash,
            })

        written = 0
        for relationship_type, rows in rows_by_type.items():
            # edge_id prevents collapsing distinct parallel relationships.
            query = (
                "UNWIND $rows AS row "
                "MATCH (source:KBNode {id: row.source}), (target:KBNode {id: row.target}) "
                f"MERGE (source)-[relationship:`{relationship_type}` {{edge_id: row.edge_id}}]->(target) "
                "SET relationship.attributes_json = row.attributes_json, "
                "relationship.source_snapshot = row.source_snapshot, "
                "relationship.source_sha256 = row.source_sha256"
            )
            for batch in self._chunks(rows):
                session.run(query, rows=batch).consume()
                written += len(batch)
        return written

    def _validate_migration(self, source_nodes: int, source_edges: int, target_nodes: int,
                            target_edges: int, migration_id: str) -> bool:
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(
            self.settings.neo4j_uri,
            auth=self.settings.neo4j_auth,
        )
        try:
            with driver.session(database=self.settings.neo4j_database) as session:
                migrated_nodes = session.run(
                    "MATCH (n:KBNode {source_snapshot: $migration_id}) RETURN count(n) AS count",
                    migration_id=migration_id,
                ).single()["count"]
                migrated_edges = session.run(
                    "MATCH ()-[r {source_snapshot: $migration_id}]->() RETURN count(r) AS count",
                    migration_id=migration_id,
                ).single()["count"]
        finally:
            driver.close()
        # The snapshot-scoped count supports incremental reruns while the total
        # count fields in the report make coexistence with pre-existing data explicit.
        return migrated_nodes == source_nodes and migrated_edges == source_edges and target_nodes >= source_nodes and target_edges >= source_edges

    def _ensure_schema(self, session: Any) -> None:
        for statement in _SCHEMA:
            session.run(statement).consume()

    def _backup_snapshot(self, migration_id: str) -> None:
        self.report_dir.mkdir(parents=True, exist_ok=True)
        backup = self.report_dir / f"{migration_id}.graph.json"
        if not backup.exists():
            shutil.copy2(self.snapshot, backup)

    def _write_report(self, report: MigrationReport) -> None:
        self.report_dir.mkdir(parents=True, exist_ok=True)
        path = self.report_dir / f"{report.migration_id}.json"
        path.write_text(json.dumps(asdict(report), indent=2), encoding="utf-8")

    def _report(self, migration_id: str, source_hash: str, source_nodes: int, source_edges: int,
                target_nodes: int, target_edges: int, nodes_written: int, edges_written: int,
                validation_passed: bool, started: datetime, *, dry_run: bool) -> MigrationReport:
        return MigrationReport(
            migration_id=migration_id,
            source_path=str(self.snapshot),
            source_sha256=source_hash,
            source_nodes=source_nodes,
            source_edges=source_edges,
            neo4j_nodes=target_nodes,
            neo4j_edges=target_edges,
            nodes_written=nodes_written,
            edges_written=edges_written,
            validation_passed=validation_passed,
            started_at=started.isoformat(),
            completed_at=datetime.now(UTC).isoformat(),
            dry_run=dry_run,
        )

    def _chunks(self, rows: list[dict[str, Any]]) -> Iterator[list[dict[str, Any]]]:
        for index in range(0, len(rows), self.batch_size):
            yield rows[index:index + self.batch_size]

    @staticmethod
    def _checksum(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as file:
            for block in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _edge_id(source: str, target: str, relationship_type: str, attributes_json: str) -> str:
        raw = "\x00".join((source, target, relationship_type, attributes_json))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
