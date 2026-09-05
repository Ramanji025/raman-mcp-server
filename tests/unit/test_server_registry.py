"""Verify the FastMCP server registers all tools, resources and prompts.

Skipped automatically when optional serving deps (fastmcp/qdrant/langgraph)
are not installed, so the offline unit suite stays green.
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastmcp")
pytest.importorskip("qdrant_client")
pytest.importorskip("langgraph")


async def test_registry_exposes_expected_surface():
    from mcp_kb.server import mcp

    tools = {t.name for t in await mcp.list_tools()}
    prompts = {p.name for p in await mcp.list_prompts()}
    templates = {rt.uri_template for rt in await mcp.list_resource_templates()}

    assert {
        "ask",
        "explain_service", "find_endpoint", "trace_business_flow",
        "impact_analysis", "analyze_defect", "implement_feature",
        "search_domain_knowledge", "generate_architecture_summary",
        "onboarding_assistant", "inspect_application", "inspect_service",
        "ask", "rate_answer", "learning_status",
    } <= tools
    assert {"chat", "investigate_defect", "plan_feature", "inspect_platform"} <= prompts
    # The per-service resource is registered as a URI template.
    assert any("kb://service/" in t for t in templates)
