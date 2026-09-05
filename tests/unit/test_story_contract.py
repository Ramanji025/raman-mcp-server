"""Story AC → graph mapping without Neo4j."""
from __future__ import annotations

from mcp_kb.nlp.story_contract import build_story_contract
from mcp_kb.tools.query_router import classify_question


def test_when_then_maps_endpoint_and_flags_missing_test():
    contract = build_story_contract(
        "Given a customer When POST /api/orders Then the order is created",
        endpoints=[{
            "name": "createOrder", "path": "/api/orders", "http_method": "POST",
            "service": "rx-order-service", "file": "OrderController.java",
        }],
        methods=[{
            "name": "create", "class": "OrderService", "service": "rx-order-service",
            "file": "OrderService.java",
        }],
        tests=[],
    )
    kinds = [c.kind for c in contract.clauses]
    assert "when" in kinds and "then" in kinds
    assert contract.files_to_touch
    assert contract.missing_tests
    md = contract.as_markdown()
    assert "Acceptance criteria" in md
    assert "Tests to write" in md


def test_inspect_routes_to_application_or_service():
    app = classify_question("inspect the application health", services=["rx-order-service"])
    assert app.handler == "inspect_application"
    one = classify_question(
        "inspect service rx-order-service", services=["rx-order-service"],
    )
    assert one.handler == "inspect_service"
    assert one.service_name == "rx-order-service"
