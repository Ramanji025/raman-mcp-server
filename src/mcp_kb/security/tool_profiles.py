"""Agent-facing tool profiles (Phase 1).

Mirrors codebase-memory-mcp's ALL / ANALYSIS / SCOUT tiers: a coarse,
config-driven restriction on which MCP tools a session may call, independent
of RBAC's per-repo policy. Set via ``MCP_KB_TOOL_PROFILE`` env var.

- ALL: every registered tool (default; unchanged behaviour).
- ANALYSIS: read-only exploration/query tools only — no write tools
  (analyse_exception, classify_incident, manage_adr writes, rate_answer).
- SCOUT: minimal discovery set for fast, cheap, provisional lookups.
"""
from __future__ import annotations

# Read-only tools safe for restricted/agentic contexts (no incident writes,
# no learning-rating writes, no ADR mutation).
ANALYSIS_TOOLS: frozenset[str] = frozenset({
    "ask", "learning_status", "explain_service", "find_endpoint",
    "trace_business_flow", "impact_analysis", "analyze_defect",
    "implement_feature", "search_domain_knowledge", "generate_architecture_summary",
    "inspect_application", "inspect_service", "explain_business_capability",
    "service_dependents", "service_failure_impact", "onboarding_assistant",
    "get_api_contract", "get_execution_flow", "compare_branches", "full_kt",
    "security_audit", "dependency_report", "schema_analysis", "find_antipatterns",
    "kafka_topology", "generate_openapi", "test_coverage_map", "get_method_detail",
    "analyze_user_story", "cross_service_impact", "explain_code", "trace_call_chain",
    "find_similar_code", "explain_exception_flow", "list_indexed_files",
    "get_defect_changes", "find_callers", "find_callees", "call_graph",
    "trace_execution_path", "find_api_path", "dependency_analysis", "search_code",
    "explain_architecture", "find_related_incidents", "compare_versions",
    "query_graph", "check_index_coverage", "get_file_outline", "find_dead_code",
    "compare_graphs",
})

# Minimal, cheap, discovery-only set — narrow provisional lookups before
# committing to a full investigation.
SCOUT_TOOLS: frozenset[str] = frozenset({
    "ask", "find_endpoint", "search_domain_knowledge", "search_code",
    "list_indexed_files", "get_file_outline", "check_index_coverage",
    "generate_architecture_summary", "explain_service",
})

TOOL_PROFILES: dict[str, frozenset[str] | None] = {
    "ALL": None,  # None = no restriction
    "ANALYSIS": ANALYSIS_TOOLS,
    "SCOUT": SCOUT_TOOLS,
}


def allowed_tools_for_profile(profile: str) -> frozenset[str] | None:
    """Return the allow-set for a profile name, or None if unrestricted/unknown."""
    return TOOL_PROFILES.get(profile.upper(), None)
