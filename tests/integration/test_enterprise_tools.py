"""Integration tests for the enterprise call-graph, dependency, and RCA tools
added on top of KnowledgeService (find_callers, find_callees,
trace_execution_path, find_api_path, dependency_analysis, explain_architecture,
analyse_exception, classify_incident, find_related_incidents).

Requires Qdrant (KnowledgeService always constructs a HybridRetriever/
VectorIndexer in __init__, even for tools that are purely graph-based), so
the whole module is skipped when Qdrant is unreachable — matching the
existing convention in test_ingestion_pipeline.py.

A small synthetic multi-service graph is built directly against
``KnowledgeService.graph`` (the same public GraphStorePort attribute the
tools operate on), independent of the Java parser's exact fixture-repo
output, so these tests exercise the tool logic deterministically.
"""
from __future__ import annotations

import pytest

from mcp_kb.models import EdgeType, GraphEdge, GraphNode, NodeType

from ..conftest import qdrant_available

pytestmark = pytest.mark.skipif(not qdrant_available(), reason="Qdrant not running")


@pytest.fixture()
def kb_service(settings, monkeypatch):
    """A KnowledgeService backed by a unique, disposable Qdrant collection set."""
    monkeypatch.setenv("QDRANT_COLLECTION_PREFIX", "mcpkbtest_enterprise")
    settings.qdrant_collection_prefix = "mcpkbtest_enterprise"

    from mcp_kb.tools.knowledge_service import KnowledgeService

    svc = KnowledgeService(settings)
    _seed_multi_service_graph(svc.graph)
    yield svc

    # Best-effort cleanup so repeated runs don't accumulate collections.
    try:
        for content_type in ("code", "docs", "architecture", "defects", "incidents"):
            name = settings.collection_name(content_type)
            if svc.retriever.store.client.collection_exists(name):
                svc.retriever.store.client.delete_collection(name)
    except Exception:
        pass


def _seed_multi_service_graph(graph) -> None:
    nodes = [
        GraphNode(id="service:rx-order:rx-order", type=NodeType.SERVICE,
                  name="rx-order", service="rx-order"),
        GraphNode(id="service:rx-pricing:rx-pricing", type=NodeType.SERVICE,
                  name="rx-pricing", service="rx-pricing"),
        GraphNode(id="controller:rx-order:OrderController", type=NodeType.CONTROLLER,
                  name="OrderController", service="rx-order"),
        GraphNode(id="endpoint:rx-order:POST /orders", type=NodeType.ENDPOINT,
                  name="POST /orders", service="rx-order",
                  attributes={"path": "/orders", "http_method": "POST",
                              "controller": "OrderController", "handler": "createOrder"}),
        GraphNode(id="method:rx-order:OrderController.createOrder", type=NodeType.METHOD,
                  name="createOrder", service="rx-order",
                  attributes={"class": "OrderController",
                              "called_methods": ["OrderService.validateOrder"]}),
        GraphNode(id="method:rx-order:OrderService.validateOrder", type=NodeType.METHOD,
                  name="validateOrder", service="rx-order",
                  attributes={"class": "OrderService", "file": "OrderService.java",
                              "start_line": 10, "end_line": 30,
                              "called_methods": ["orderRepository.save"],
                              "delegates_to_repos": ["OrderRepository"],
                              "thrown_exceptions": ["ValidationException"]}),
        GraphNode(id="repository:rx-order:OrderRepository", type=NodeType.REPOSITORY,
                  name="OrderRepository", service="rx-order",
                  attributes={"managed_entity": "Order", "query_methods": []}),
    ]
    edges = [
        GraphEdge(src="controller:rx-order:OrderController",
                  dst="endpoint:rx-order:POST /orders", type=EdgeType.EXPOSES),
        GraphEdge(src="endpoint:rx-order:POST /orders",
                  dst="method:rx-order:OrderController.createOrder", type=EdgeType.DELEGATES_TO),
        GraphEdge(src="method:rx-order:OrderController.createOrder",
                  dst="method:rx-order:OrderService.validateOrder", type=EdgeType.CALLS),
        GraphEdge(src="method:rx-order:OrderService.validateOrder",
                  dst="repository:rx-order:OrderRepository", type=EdgeType.DELEGATES_TO),
        GraphEdge(src="service:rx-order:rx-order", dst="service:rx-pricing:rx-pricing",
                  type=EdgeType.CALLS),
    ]
    graph.add_many(nodes, edges)


def test_find_callers_locates_upstream_caller(kb_service):
    resp = kb_service.find_callers("OrderService", "validateOrder")
    assert resp.data["total_callers"] >= 1
    assert any(sm["name"] == "createOrder" for sm in resp.data["service_methods"])


def test_find_callees_locates_downstream_repository(kb_service):
    resp = kb_service.find_callees("OrderService", "validateOrder")
    assert resp.data["total_callees"] >= 1
    assert any(c["type"] == "Repository" for c in resp.data["callees"])
    assert "ValidationException" in resp.data["thrown_exceptions"]


def test_find_callers_reports_not_found_for_unknown_method(kb_service):
    resp = kb_service.find_callers("NoSuchClass", "noSuchMethod")
    assert "No knowledge-graph node matched" in resp.summary
    assert resp.data.get("matches") == []


def test_trace_execution_path_finds_controller_to_repository(kb_service):
    resp = kb_service.trace_execution_path("OrderController", "OrderRepository")
    assert resp.data["path_count"] >= 1
    names_in_first_path = [n["name"] for n in resp.data["paths"][0]]
    assert "OrderRepository" in names_in_first_path


def test_find_api_path_between_services(kb_service):
    resp = kb_service.find_api_path("rx-order", "rx-pricing")
    assert resp.data["direct_dependency"] is True


def test_dependency_analysis_includes_service_graph(kb_service):
    resp = kb_service.dependency_analysis("rx-order")
    assert resp.data["depends_on_services"] == ["rx-pricing"]


def test_explain_architecture_reports_seeded_services(kb_service):
    resp = kb_service.explain_architecture()
    assert "rx-order" in resp.data["services"]
    assert "rx-pricing" in resp.data["services"]


def test_classify_incident_persists_and_returns_severity(kb_service):
    resp = kb_service.classify_incident(
        "Checkout is failing for all customers", service_name="rx-order",
        customer_impact="critical", services_affected=2,
    )
    assert resp.data["incident_id"]
    assert resp.data["severity"]["severity"] in ("P1", "P2", "P3", "P4")


def test_find_related_incidents_after_classify(kb_service):
    kb_service.classify_incident("Payment gateway timeout under load",
                                 service_name="rx-order", customer_impact="high")
    resp = kb_service.find_related_incidents("payment gateway timeout")
    assert resp.data["count"] >= 1


def test_analyse_exception_persists_incident_with_root_cause(kb_service):
    trace = (
        "java.lang.NullPointerException: order is null\n"
        "\tat com.rx.order.service.OrderService.validateOrder(OrderService.java:20)\n"
    )
    resp = kb_service.analyse_exception(stack_trace=trace, service_name="rx-order")
    assert resp.data["incident_id"]
    assert "validateOrder" in resp.data["root_cause"]
    assert resp.data["severity"]["severity"] in ("P1", "P2", "P3", "P4")
