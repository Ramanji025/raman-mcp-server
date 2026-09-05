"""One-off generator: splits tools/knowledge_service.py into a mixin-based package.

Safe by construction: every method's source text is copied verbatim (no manual
retyping), and we assert the set of methods placed into groups exactly matches
the set of methods found in the original file before writing anything.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path("src/mcp_kb/tools/knowledge_service.py")
OUT_DIR = Path("src/mcp_kb/tools/knowledge_service")

GROUPS: dict[str, tuple[str, list[str]]] = {
    "_core": ("CoreMixin", [
        "__init__", "_resolve_service", "_resolve_entity_or_table", "_not_found",
    ]),
    "_service_overview": ("ServiceOverviewMixin", [
        "explain_service", "find_endpoint", "_build_invocation_chain",
        "trace_business_flow", "impact_analysis", "_find_callers",
        "_render_service_md", "_render_endpoints_md", "_render_impact_md",
        "_render_execution_flow_md",
    ]),
    "_defect_feature": ("DefectFeatureMixin", [
        "analyze_defect", "implement_feature",
    ]),
    "_search": ("SearchMixin", [
        "search_domain_knowledge", "_render_search_md",
    ]),
    "_architecture": ("ArchitectureMixin", [
        "generate_architecture_summary", "_render_architecture_md",
        "_mermaid_dependency_graph", "compare_branches", "_render_branch_diff_md",
        "explain_architecture", "kafka_topology", "_render_kafka_topology_md",
        "_kafka_mermaid", "compare_versions",
    ]),
    "_inspection": ("InspectionMixin", [
        "inspect_service", "inspect_application", "_inspection_snapshot",
        "_smell_severity",
    ]),
    "_capabilities": ("CapabilitiesMixin", [
        "explain_business_capability", "_match_capabilities",
        "_payment_validation_points", "_render_capability_md",
    ]),
    "_dependents": ("DependentsMixin", [
        "service_dependents", "service_failure_impact",
        "_render_service_dependents_md", "_render_service_failure_impact_md",
    ]),
    "_onboarding": ("OnboardingMixin", [
        "onboarding_assistant", "_onboarding_steps", "_render_onboarding_md",
    ]),
    "_contracts": ("ContractsMixin", [
        "get_api_contract", "get_execution_flow", "_build_full_contract",
        "_resolve_dto", "_exception_to_status", "_render_api_contract_md",
        "generate_openapi", "_java_type_to_openapi",
    ]),
    "_kt": ("KtMixin", [
        "full_kt", "_render_full_kt_md",
    ]),
    "_quality": ("QualityMixin", [
        "security_audit", "_security_recommendations", "dependency_report",
        "schema_analysis", "find_antipatterns", "_render_security_audit_md",
        "_render_dependency_report_md", "_render_schema_analysis_md",
        "_render_antipatterns_md", "test_coverage_map", "_test_recommendations",
        "_render_test_coverage_md",
    ]),
    "_code_intel": ("CodeIntelMixin", [
        "get_method_detail", "_render_method_detail_md", "_build_signature",
        "explain_code", "_node_summary", "_method_explanation",
        "_endpoint_explanation", "_render_explain_code_md", "trace_call_chain",
        "_render_call_chain_md", "find_similar_code", "list_indexed_files",
        "find_callers", "find_callees", "trace_execution_path", "find_api_path",
        "dependency_analysis", "search_code",
    ]),
    "_rca": ("RcaMixin", [
        "analyze_user_story", "_story_contract", "_story_implementation_plan",
        "_render_story_analysis_md", "cross_service_impact",
        "_cross_service_recommendations", "_render_cross_service_md",
        "explain_exception_flow", "get_defect_changes",
        "_render_defect_changes_md", "analyse_exception", "classify_incident",
        "find_related_incidents",
    ]),
    "_ask": ("AskMixin", [
        "ask", "_ask_response", "_record_analytics", "rate_answer",
        "learning_status", "_ask_entity_names", "_dispatch_ask",
        "_resolve_method_node",
    ]),
}

# Helper functions used by more than one mixin -> live in _helpers.py.
HELPER_FUNCS = ["_intent_collections", "_intent_graph_hops", "_infer_method_purpose"]
HELPER_USERS = {
    "_ask": ["_intent_collections", "_intent_graph_hops"],
    "_code_intel": ["_infer_method_purpose"],
    "_rca": ["_infer_method_purpose"],
}


def shift_dots(text: str) -> str:
    # from .. -> from ... (module moved one package level deeper)
    text = re.sub(r"^(\s*)from \.\.(?!\.)", r"\1from ...", text, flags=re.MULTILINE)
    # from . -> from ..
    text = re.sub(r"^(\s*)from \.(?!\.)", r"\1from ..", text, flags=re.MULTILINE)
    return text


def main() -> None:
    source = SRC.read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    tree = ast.parse(source)

    class_node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "KnowledgeService")
    class_start_line = class_node.lineno  # 1-indexed, the "class KnowledgeService:" line

    header_text = "".join(lines[: class_start_line - 1])
    future_idx = header_text.index("from __future__ import annotations")
    m = re.search(r"^def _intent_collections", header_text, flags=re.MULTILINE)
    assert m, "could not locate free-function block start"
    import_block = header_text[future_idx : m.start()]
    helpers_block = header_text[m.start():]

    methods: dict[str, str] = {}
    for n in class_node.body:
        if isinstance(n, ast.FunctionDef):
            methods[n.name] = "".join(lines[n.lineno - 1 : n.end_lineno])

    grouped_names = {name for _, names in GROUPS.values() for name in names}
    assert grouped_names == set(methods), (
        f"mismatch!\nmissing from GROUPS: {set(methods) - grouped_names}\n"
        f"extra in GROUPS (not in source): {grouped_names - set(methods)}"
    )

    OUT_DIR.mkdir(exist_ok=True)

    shifted_import_block = shift_dots(import_block)
    shifted_helpers_block = shift_dots(helpers_block)

    (OUT_DIR / "_helpers.py").write_text(
        '"""Module-level helper functions shared by several KnowledgeService mixins."""\n'
        + shifted_import_block + "\n" + shifted_helpers_block,
        encoding="utf-8",
    )

    mixin_class_names: list[tuple[str, str]] = []  # (module_stem, class_name)
    for stem, (class_name, names) in GROUPS.items():
        mixin_class_names.append((stem, class_name))
        body = "".join(methods[n] for n in names)
        extra_imports = ""
        needed_helpers = HELPER_USERS.get(stem, [])
        if needed_helpers:
            extra_imports = f"from ._helpers import {', '.join(needed_helpers)}\n"
        content = (
            f'"""KnowledgeService mixin: {class_name} ({stem[1:]} domain).\n\n'
            "Auto-generated split — see scripts/_split_knowledge_service.py.\n\"\"\"\n"
            + shifted_import_block
            + extra_imports
            + "\n\n"
            + f"class {class_name}:\n"
            + body
        )
        (OUT_DIR / f"{stem}.py").write_text(content, encoding="utf-8")

    init_imports = "\n".join(
        f"from .{stem} import {cls}" for stem, cls in mixin_class_names
    )
    bases = ", ".join(cls for _, cls in mixin_class_names)
    init_content = (
        '"""The KnowledgeService: shared engine backing all MCP tools.\n\n'
        "Split into per-domain mixins (see the sibling ``_*.py`` modules); this file just\n"
        "composes them into the single public ``KnowledgeService`` class so every method\n"
        "still shares one ``self`` instance exactly as before the split.\n\"\"\"\n"
        "from __future__ import annotations\n\n"
        + init_imports + "\n\n\n"
        + f"class KnowledgeService({bases}):\n"
        + '    """Loads the graph + retriever once and serves all tool requests."""\n'
    )
    (OUT_DIR / "__init__.py").write_text(init_content, encoding="utf-8")

    print("OK — wrote", len(GROUPS) + 2, "files to", OUT_DIR)


if __name__ == "__main__":
    main()
