"""Smoke test: verify the MCP server registers its tools, resources and prompts.

Runs without Qdrant/Postgres/embeddings because it only inspects the FastMCP
registry (the KnowledgeService is built lazily on first invocation, not on import).

    python scripts/smoke_test.py
"""
from __future__ import annotations

import asyncio
import sys

EXPECTED_TOOLS = {
    "explain_service", "find_endpoint", "trace_business_flow", "impact_analysis",
    "analyze_defect", "implement_feature", "search_domain_knowledge",
    "generate_architecture_summary", "onboarding_assistant",
}
EXPECTED_PROMPTS = {"investigate_defect", "plan_feature"}


async def _main() -> int:
    from mcp_kb.server import mcp

    tools = {t.name for t in await mcp.list_tools()}
    prompts = {p.name for p in await mcp.list_prompts()}
    resources = {str(r.uri) for r in await mcp.list_resources()}
    templates = {rt.uri_template for rt in await mcp.list_resource_templates()}

    print(f"Tools     ({len(tools)}): {sorted(tools)}")
    print(f"Prompts   ({len(prompts)}): {sorted(prompts)}")
    print(f"Resources ({len(resources)}): {sorted(resources)}")
    print(f"Templates ({len(templates)}): {sorted(templates)}")

    missing_tools = EXPECTED_TOOLS - tools
    missing_prompts = EXPECTED_PROMPTS - prompts
    ok = not missing_tools and not missing_prompts
    if missing_tools:
        print(f"MISSING TOOLS: {sorted(missing_tools)}", file=sys.stderr)
    if missing_prompts:
        print(f"MISSING PROMPTS: {sorted(missing_prompts)}", file=sys.stderr)
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
