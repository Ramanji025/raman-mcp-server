"""Async enterprise Neo4j manager for graph-native platform workflows.

This layer complements the synchronous ``Neo4jGraphStore`` adapter. Use it from
async MCP tools, background workers, or API services that need explicit
transaction boundaries and graph-native traversal queries.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any

from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from ..config import Settings, get_settings
from ..logging import get_logger
from ..models import EdgeType, NodeType

log = get_logger(__name__)

_CANONICAL_LABELS = {
    "Repository", "Service", "Package", "Class", "Interface", "Method",
    "APIEndpoint", "DatabaseTable", "KafkaTopic", "Exception", "Incident",
    "BusinessCapability",
}
_VALID_LABELS = {node_type.value for node_type in NodeType} | _CANONICAL_LABELS
_VALID_RELATIONSHIPS = {edge_type.value for edge_type in EdgeType} | {"BELONGS_TO"}


class Neo4jGraphManager:
    """Async, pooled, retry-aware Neo4j access layer.

    The manager owns one ``AsyncDriver`` for its lifecycle. The Neo4j driver
    provides connection pooling and retryable managed transactions; a small
    outer retry is added only for transient driver failures during connection
    loss or leader changes.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        driver: Any | None = None,
        max_connection_pool_size: int = 50,
        connection_acquisition_timeout: float = 30.0,
        max_transaction_retry_time: float = 30.0,
        retry_attempts: int = 3,
    ) -> None:
        self._settings = settings or get_settings()
        self._driver = driver
        self._owns_driver = driver is None
        self._pool_size = max_connection_pool_size
        self._acquisition_timeout = connection_acquisition_timeout
        self._transaction_retry_time = max_transaction_retry_time
        self._retry_attempts = retry_attempts
        self._database = self._settings.neo4j_database

    async def connect(self) -> None:
        """Create and verify the pooled async driver once."""
        if self._driver is None:
            from neo4j import AsyncGraphDatabase

            self._driver = AsyncGraphDatabase.driver(
                self._settings.neo4j_uri,
                auth=self._settings.neo4j_auth,
                max_connection_pool_size=self._pool_size,
                connection_acquisition_timeout=self._acquisition_timeout,
                max_transaction_retry_time=self._transaction_retry_time,
            )
        await self._driver.verify_connectivity()
        log.info("neo4j_manager_connected", uri=self._settings.neo4j_uri,
                 database=self._database, pool_size=self._pool_size)

    async def close(self) -> None:
        """Close the driver connection if this manager owns it."""
        if self._driver is not None and self._owns_driver:
            await self._driver.close()
        self._driver = None

    async def __aenter__(self) -> Neo4jGraphManager:
        """Connect on entering the async context manager."""
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        """Close the connection on exiting the async context manager."""
        await self.close()

    @asynccontextmanager
    async def transaction(self, *, write: bool = False) -> AsyncIterator[Any]:
        """Yield an explicit transaction for atomic multi-step graph updates."""
        driver = await self._require_driver()
        async with driver.session(database=self._database) as session:
            tx = await (session.begin_transaction() if write else session.begin_transaction())
            try:
                yield tx
                await tx.commit()
            except Exception:
                await tx.rollback()
                raise

    async def create_node(
        self,
        node_id: str,
        label: str,
        properties: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Idempotently create/update a platform node using its stable id."""
        self._validate_label(label)
        props = {**(properties or {}), "id": node_id}
        props.setdefault("type", label)
        query = (
            f"MERGE (node:KBNode:`{label}` {{id: $id}}) "
            "SET node += $properties "
            "RETURN node"
        )
        record = await self._write_one(query, {"id": node_id, "properties": props})
        return self._node(record["node"])

    async def create_relationship(
        self,
        source_id: str,
        target_id: str,
        relationship_type: str,
        properties: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or update a typed relationship between existing nodes."""
        self._validate_relationship(relationship_type)
        query = (
            "MATCH (source:KBNode {id: $source_id}), (target:KBNode {id: $target_id}) "
            f"MERGE (source)-[relationship:`{relationship_type}`]->(target) "
            "SET relationship += $properties "
            "RETURN source.id AS source_id, type(relationship) AS relationship_type, "
            "target.id AS target_id, properties(relationship) AS properties"
        )
        record = await self._write_one(query, {
            "source_id": source_id,
            "target_id": target_id,
            "properties": properties or {},
        })
        if record is None:
            raise ValueError(f"relationship endpoints not found: {source_id} -> {target_id}")
        return dict(record)

    async def find_node(self, node_id: str) -> dict[str, Any] | None:
        """Return the node dict for `node_id`, or None if not present."""
        query = "MATCH (node:KBNode {id: $id}) RETURN node"
        record = await self._read_one(query, {"id": node_id})
        return self._node(record["node"]) if record else None

    async def find_callers(self, node_id: str, *, max_hops: int = 5, limit: int = 100) -> list[dict[str, Any]]:
        """Return unique inbound callers and shortest call paths to a node."""
        return await self._find_reachable(node_id, direction="in", max_hops=max_hops, limit=limit)

    async def find_callees(self, node_id: str, *, max_hops: int = 5, limit: int = 100) -> list[dict[str, Any]]:
        """Return unique outbound callees and shortest call paths from a node."""
        return await self._find_reachable(node_id, direction="out", max_hops=max_hops, limit=limit)

    async def impact_analysis(self, node_id: str, *, max_hops: int = 3, limit: int = 250) -> dict[str, Any]:
        """Find transitive reverse dependencies and summarize affected services."""
        self._validate_hops(max_hops)
        query = (
            "MATCH path = (impacted:KBNode)-[:CALLS|DELEGATES_TO|READS|WRITES|DEPENDS_ON|PUBLISHES|CONSUMES*1..10]->(target:KBNode {id: $id}) "
            "WHERE length(path) <= $max_hops "
            "WITH impacted, min(length(path)) AS hops "
            "RETURN impacted, hops ORDER BY hops, impacted.service, impacted.name LIMIT $limit"
        )
        records = await self._read_many(query, {"id": node_id, "max_hops": max_hops, "limit": limit})
        impacted = [{**self._node(record["impacted"]), "hops": record["hops"]} for record in records]
        services = sorted({item.get("service") for item in impacted if item.get("service")})
        return {
            "target_id": node_id,
            "max_hops": max_hops,
            "total_impacted": len(impacted),
            "services": services,
            "nodes": impacted,
        }

    async def trace_execution_path(
        self, source_id: str, target_id: str, *, max_hops: int = 8, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Return bounded call/data/event paths between two graph nodes."""
        self._validate_hops(max_hops)
        query = (
            "MATCH path = (source:KBNode {id: $source_id})-[:CALLS|DELEGATES_TO|READS|WRITES|PUBLISHES|CONSUMES*1..10]->(target:KBNode {id: $target_id}) "
            "WHERE length(path) <= $max_hops "
            "RETURN [node IN nodes(path) | {id: node.id, type: node.type, name: node.name, service: node.service}] AS nodes, "
            "[relationship IN relationships(path) | {type: type(relationship), seq: relationship.seq, via: relationship.via}] AS relationships "
            "ORDER BY length(path) LIMIT $limit"
        )
        records = await self._read_many(query, {
            "source_id": source_id,
            "target_id": target_id,
            "max_hops": max_hops,
            "limit": limit,
        })
        return [dict(record) for record in records]

    async def save_execution_flow(
        self,
        flow_id: str,
        *,
        service: str,
        name: str,
        endpoint_id: str,
        steps: Iterable[dict[str, Any]],
        properties: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist an execution flow and sequence-numbered step relationships atomically.

        Each step requires ``node_id``. Optional ``relationship_type`` defaults
        to ``CALLS`` and optional ``properties`` are merged into the edge.
        """
        step_rows = []
        for seq, step in enumerate(steps, start=1):
            node_id = step.get("node_id")
            if not node_id:
                raise ValueError("every execution-flow step requires node_id")
            relationship_type = str(step.get("relationship_type", "CALLS"))
            self._validate_relationship(relationship_type)
            step_rows.append({
                "node_id": node_id,
                "relationship_type": relationship_type,
                "properties": {**step.get("properties", {}), "role": "execution-step", "seq": seq},
            })

        flow_props = {**(properties or {}), "id": flow_id, "type": NodeType.EXECUTION_FLOW.value,
                      "name": name, "service": service, "endpoint_id": endpoint_id,
                      "sequenced_steps": [{"seq": row["properties"]["seq"], "node_id": row["node_id"]} for row in step_rows]}
        driver = await self._require_driver()

        async def write(tx: Any) -> dict[str, Any]:
            flow = await tx.run(
                "MERGE (flow:KBNode:ExecutionFlow {id: $id}) SET flow += $properties RETURN flow",
                id=flow_id, properties=flow_props,
            )
            if await (await tx.run("MATCH (:KBNode {id: $flow_id}), (:KBNode {id: $endpoint_id}) RETURN 1 AS exists", flow_id=flow_id, endpoint_id=endpoint_id)).single() is None:
                raise ValueError(f"execution flow endpoint not found: {endpoint_id}")
            await tx.run(
                "MATCH (flow:KBNode {id: $flow_id}), (endpoint:KBNode {id: $endpoint_id}) "
                "MERGE (flow)-[relationship:DECLARED_IN]->(endpoint) "
                "SET relationship.role = 'entry'",
                flow_id=flow_id, endpoint_id=endpoint_id,
            )
            for row in step_rows:
                await tx.run(
                    "MATCH (flow:KBNode {id: $flow_id}), (step:KBNode {id: $step_id}) "
                    f"MERGE (flow)-[relationship:`{row['relationship_type']}`]->(step) "
                    "SET relationship += $properties",
                    flow_id=flow_id, step_id=row["node_id"], properties=row["properties"],
                )
            record = await flow.single()
            return self._node(record["flow"])

        return await self._execute_write(driver, write)

    async def _find_reachable(self, node_id: str, *, direction: str, max_hops: int, limit: int) -> list[dict[str, Any]]:
        self._validate_hops(max_hops)
        pattern = (
            "(target:KBNode {id: $id})<-[:CALLS|DELEGATES_TO*1..10]-(node:KBNode)"
            if direction == "in" else
            "(target:KBNode {id: $id})-[:CALLS|DELEGATES_TO*1..10]->(node:KBNode)"
        )
        query = (
            f"MATCH path = {pattern} WHERE length(path) <= $max_hops "
            "WITH node, min(length(path)) AS hops "
            "RETURN node, hops ORDER BY hops, node.service, node.name LIMIT $limit"
        )
        records = await self._read_many(query, {"id": node_id, "max_hops": max_hops, "limit": limit})
        return [{**self._node(record["node"]), "hops": record["hops"]} for record in records]

    async def _write_one(self, query: str, params: dict[str, Any]) -> Any:
        driver = await self._require_driver()
        return await self._execute_write(driver, lambda tx: self._run_one(tx, query, params))

    async def _read_one(self, query: str, params: dict[str, Any]) -> Any:
        records = await self._read_many(query, params)
        return records[0] if records else None

    async def _read_many(self, query: str, params: dict[str, Any]) -> list[Any]:
        driver = await self._require_driver()

        async def read(tx: Any) -> list[Any]:
            result = await tx.run(query, **params)
            return [record async for record in result]

        return await self._execute_read(driver, read)

    @staticmethod
    async def _run_one(tx: Any, query: str, params: dict[str, Any]) -> Any:
        result = await tx.run(query, **params)
        return await result.single()

    async def _execute_write(self, driver: Any, work: Any) -> Any:
        return await self._retry("neo4j_write", self._managed_transaction(driver, work, write=True))

    async def _execute_read(self, driver: Any, work: Any) -> Any:
        return await self._retry("neo4j_read", self._managed_transaction(driver, work, write=False))

    def _managed_transaction(self, driver: Any, work: Any, *, write: bool):
        async def run() -> Any:
            async with driver.session(database=self._database) as session:
                return await (session.execute_write(work) if write else session.execute_read(work))
        return run

    async def _retry(self, operation: str, work: Any) -> Any:
        from neo4j.exceptions import ServiceUnavailable, SessionExpired, TransientError

        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type((ServiceUnavailable, SessionExpired, TransientError)),
            wait=wait_exponential_jitter(initial=0.25, max=4),
            stop=stop_after_attempt(self._retry_attempts),
            reraise=True,
        ):
            with attempt:
                result = await work()
                if attempt.retry_state.attempt_number > 1:
                    log.info("neo4j_operation_recovered", operation=operation,
                             attempt=attempt.retry_state.attempt_number)
                return result
        raise RuntimeError(f"Neo4j retry loop exited unexpectedly: {operation}")

    async def _require_driver(self) -> Any:
        if self._driver is None:
            await self.connect()
        return self._driver

    @staticmethod
    def _node(value: Any) -> dict[str, Any]:
        properties = dict(value)
        properties["labels"] = sorted(value.labels)
        return properties

    @staticmethod
    def _validate_hops(max_hops: int) -> None:
        if not 1 <= max_hops <= 10:
            raise ValueError("max_hops must be between 1 and 10")

    @staticmethod
    def _validate_label(label: str) -> None:
        if label not in _VALID_LABELS:
            raise ValueError(f"unsupported Neo4j node label: {label}")

    @staticmethod
    def _validate_relationship(relationship_type: str) -> None:
        if relationship_type not in _VALID_RELATIONSHIPS:
            raise ValueError(f"unsupported Neo4j relationship type: {relationship_type}")
