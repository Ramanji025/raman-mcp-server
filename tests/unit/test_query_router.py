"""Unit tests for natural-language query routing (no Qdrant/Neo4j required)."""
from __future__ import annotations

import pytest

from mcp_kb.tools.query_router import classify_question

SERVICES = ["rx-order-service", "rx-pricing-service", "inventory-service"]


@pytest.mark.parametrize("question,handler", [
    ("review DE194624", "get_defect_changes"),
    ("what changed in US645138", "get_defect_changes"),
    ("Give me a whole-system architecture overview", "explain_architecture"),
    ("explain rx-order-service", "explain_service"),
    ("Find the endpoint /api/orders", "find_endpoint"),
    ("Where is POST /api/v1/payments?", "find_endpoint"),
    ("Orders stuck in PENDING after deploy — root cause?", "analyze_defect"),
    ("I need to add partial refunds to orders", "implement_feature"),
    ("What breaks if I change the Order entity?", "impact_analysis"),
    ("I'm new and will work on payments, where do I start?", "onboarding_assistant"),
    ("How does eligibility checking work in our platform?", "search_code"),
])
def test_classify_routes_common_nl_questions(question, handler):
    route = classify_question(question, services=SERVICES)
    assert route.handler == handler


def test_ticket_id_extracted():
    route = classify_question("please review DE194624 for me", services=SERVICES)
    assert route.ticket_id == "DE194624"


def test_service_name_extracted_for_overview():
    route = classify_question(
        "tell me about rx-order-service", services=SERVICES,
    )
    assert route.service_name == "rx-order-service"
    assert route.handler == "explain_service"


def test_generic_question_uses_hybrid_search():
    route = classify_question(
        "How do we validate a customer before checkout?", services=SERVICES,
    )
    assert route.handler == "search_code"
    assert route.intent == "general_search"


def test_empty_question_does_not_crash():
    route = classify_question("  ", services=SERVICES)
    assert route.intent == "empty"
    assert route.handler == "search_code"


def test_prompts_have_no_required_arguments():
    """Slash menus must not prompt for extra mandatory text."""
    pytest.importorskip("fastmcp")
    pytest.importorskip("qdrant_client")
    pytest.importorskip("langgraph")
    import asyncio

    from mcp_kb.server import mcp

    async def _check():
        prompts = await mcp.list_prompts()
        for p in prompts:
            required = [a for a in (p.arguments or []) if getattr(a, "required", False)]
            assert required == [], f"{p.name} still requires {required}"

    asyncio.run(_check())
