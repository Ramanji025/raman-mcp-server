"""Unit tests for the tree-sitter Java parser."""
from __future__ import annotations

from pathlib import Path

from mcp_kb.ingestion.parsers.java_parser import JavaParser
from mcp_kb.models import ContentType, EdgeType, NodeType, SourceFile


def _source(settings, repo_root: Path, rel: str) -> SourceFile:
    path = repo_root / rel
    return SourceFile(
        repo="rx-order-service", rel_path=rel, abs_path=str(path),
        content_type=ContentType.JAVA, sha256="x", size_bytes=path.stat().st_size,
    )


def test_controller_and_endpoints(settings, fixture_repo):
    parser = JavaParser(settings)
    src = _source(settings, fixture_repo,
                  "src/main/java/com/rx/order/web/OrderController.java")
    result = parser.parse(src)

    controllers = [n for n in result.nodes if n.type == NodeType.CONTROLLER]
    endpoints = [n for n in result.nodes if n.type == NodeType.ENDPOINT]
    assert len(controllers) == 1
    assert controllers[0].name == "OrderController"

    paths = {(n.attributes["http_method"], n.attributes["path"]) for n in endpoints}
    assert ("GET", "/api/orders/{id}") in paths
    assert ("GET", "/api/orders") in paths
    assert ("POST", "/api/orders") in paths
    assert ("DELETE", "/api/orders/{id}") in paths

    fields = [n for n in result.nodes if n.type == NodeType.FIELD]
    assert any(n.name == "orderService" for n in fields)
    methods = {n.name for n in result.nodes if n.type == NodeType.METHOD}
    assert {"getOrder", "listOrders", "createOrder", "cancelOrder"} <= methods
    params = [n for n in result.nodes if n.type == NodeType.PARAMETER]
    assert any(n.name == "id" and n.attributes.get("method") == "getOrder" for n in params)
    anns = {n.name for n in result.nodes if n.type == NodeType.ANNOTATION_USAGE}
    assert "RestController" in anns
    assert "GetMapping" in anns
    assert "PathVariable" in anns
    assert any(n.type == NodeType.CONSTRUCTOR for n in result.nodes)
    assert any(e.type == EdgeType.HAS_FIELD for e in result.edges)
    assert any(e.type == EdgeType.HAS_PARAMETER for e in result.edges)
    assert any(e.type == EdgeType.HAS_ANNOTATION for e in result.edges)

    # EXPOSES edges connect controller to each endpoint.
    exposes = [e for e in result.edges if e.type == EdgeType.EXPOSES]
    assert len(exposes) == len(endpoints) == 4


def test_entity_and_table_mapping(settings, fixture_repo):
    parser = JavaParser(settings)
    src = _source(settings, fixture_repo,
                  "src/main/java/com/rx/order/domain/Order.java")
    result = parser.parse(src)

    entities = [n for n in result.nodes if n.type == NodeType.ENTITY]
    tables = [n for n in result.nodes if n.type == NodeType.TABLE]
    assert entities and entities[0].name == "Order"
    assert tables and tables[0].name == "orders"
    assert any(e.type == EdgeType.MAPS_TO for e in result.edges)


def test_repository_queries_entity(settings, fixture_repo):
    parser = JavaParser(settings)
    src = _source(settings, fixture_repo,
                  "src/main/java/com/rx/order/repo/OrderRepository.java")
    result = parser.parse(src)
    repos = [n for n in result.nodes if n.type == NodeType.REPOSITORY]
    assert repos and repos[0].name == "OrderRepository"
    assert any(e.type == EdgeType.QUERIES for e in result.edges)


def test_kafka_producer_and_consumer(settings, fixture_repo):
    parser = JavaParser(settings)
    src = _source(settings, fixture_repo,
                  "src/main/java/com/rx/order/messaging/OrderEventHandler.java")
    result = parser.parse(src)
    edges = {e.type for e in result.edges}
    assert EdgeType.CONSUMES_FROM in edges
    assert EdgeType.PRODUCES_TO in edges
    topics = {n.name for n in result.nodes if n.type == NodeType.TOPIC}
    assert "payment.completed" in topics
    assert "order.fulfilled" in topics


def test_logical_blocks_and_generic_class(settings, tmp_path):
    java = tmp_path / "GuardService.java"
    java.write_text(
        """
package com.rx.order.svc;
import org.springframework.stereotype.Service;
@Service
public class GuardService {
    public String decide(int n) {
        if (n <= 0) {
            return "empty";
        }
        try {
            return String.valueOf(n);
        } catch (RuntimeException ex) {
            return "err";
        }
    }
}
""",
        encoding="utf-8",
    )
    parser = JavaParser(settings)
    src = SourceFile(
        repo="rx-order-service", rel_path="GuardService.java", abs_path=str(java),
        content_type=ContentType.JAVA, sha256="x", size_bytes=java.stat().st_size,
    )
    result = parser.parse(src)
    kinds = {n.name for n in result.nodes if n.type == NodeType.LOGICAL_BLOCK}
    assert "if" in kinds
    assert "try" in kinds
    assert "catch" in kinds
    assert any(e.type == EdgeType.CONTAINS_BLOCK for e in result.edges)
    assert any(n.type == NodeType.SERVICE_LAYER and n.name == "GuardService" for n in result.nodes)

