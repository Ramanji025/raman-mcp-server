"""FastMCP server exposing the nine knowledge-engine tools.

Run with the stdio transport (default, for IDE/agent clients) or HTTP:
    python -m mcp_kb.server                 # stdio
    MCP_KB_TRANSPORT=http python -m mcp_kb.server
"""
from __future__ import annotations

import functools
import hashlib
import json
import threading
import time
from typing import TYPE_CHECKING

from fastmcp import FastMCP
from fastmcp.tools.tool import ToolAnnotations

from .config import get_settings
from .logging import get_logger

if TYPE_CHECKING:
    from .tools.knowledge_service import KnowledgeService

log = get_logger(__name__)

# --------------------------------------------------------------------------- #
# P1.4: Query-level response cache (TTL=120s, max 256 entries)
# Keyed on (tool_name, normalized_args) — invalidated on rate_answer calls.
# --------------------------------------------------------------------------- #
_CACHE_TTL_S = 120
_CACHE_MAX = 256

class _ResponseCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store: dict[str, tuple[str, float]] = {}  # key -> (result, expiry)

    def _evict(self) -> None:
        now = time.monotonic()
        expired = [k for k, (_, exp) in self._store.items() if exp <= now]
        for k in expired:
            del self._store[k]
        if len(self._store) > _CACHE_MAX:
            oldest = sorted(self._store.items(), key=lambda kv: kv[1][1])
            for k, _ in oldest[:len(self._store) - _CACHE_MAX]:
                del self._store[k]

    def get(self, key: str) -> str | None:
        """Return the cached value for `key`, or None if missing/expired."""
        with self._lock:
            entry = self._store.get(key)
            if entry and time.monotonic() < entry[1]:
                return entry[0]
            return None

    def set(self, key: str, value: str) -> None:
        """Cache `value` under `key` with the configured TTL, evicting stale/excess entries first."""
        with self._lock:
            self._evict()
            self._store[key] = (value, time.monotonic() + _CACHE_TTL_S)

    def invalidate_all(self) -> None:
        """Clear every cached entry."""
        with self._lock:
            self._store.clear()


_cache = _ResponseCache()


def _cache_key(tool: str, **kwargs) -> str:
    payload = json.dumps({"tool": tool, **kwargs}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()

# Annotations applied to every read-only knowledge tool.
# readOnlyHint   → tool never modifies state; client may cache freely
# idempotentHint → same args always return same result; safe to deduplicate calls
# openWorldHint  → tool is self-contained (knowledge graph + local git); no web/terminal needed
_RO = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)

# readOnlyHint=False → tool persists new state (an incident record); still
# self-contained (no network/terminal) and safe to call repeatedly.
_RW = ToolAnnotations(readOnlyHint=False, idempotentHint=False, openWorldHint=False)

mcp: FastMCP = FastMCP(
    name="microservices-knowledge-base",
    instructions=(
        "Enterprise microservices knowledge platform (project code and architecture graph). "
        "For ANY natural-language user message, call `ask` first and pass the message "
        "unchanged as `question`. Do not ask the user to pick a tool or fill extra prompt "
        "fields. Specialized tools are optional follow-ups after `ask` when you need a "
        "narrow structured result. After a good or bad answer, call `rate_answer` "
        "with 5 or 1 so retrieval and aliases improve over time. "
        "MCP prompts are workflow hints only — they take no "
        "arguments; apply them to the current user message. "
        "Most tools are read-only; analyse_exception and classify_incident persist incidents."
    ),
)


@functools.lru_cache(maxsize=1)
def _service() -> KnowledgeService:
    from .tools.knowledge_service import KnowledgeService
    return KnowledgeService(get_settings())


def _prewarm() -> None:
    """Load embedder + graph in a background thread so first tool call is fast."""
    import threading
    threading.Thread(target=_service, daemon=True, name="mcp-kb-prewarm").start()


def _emit(response) -> str:
    """Return pre-rendered Markdown so the LLM passes it through without re-processing.
    Returning raw JSON forces the LLM to reformat it, wasting tokens on every call."""
    md = response.markdown or response.summary
    if response.citations:
        refs = "\n".join(
            f"- `{c.get('file','?')}` line {c.get('line','?')}: {c.get('text','')[:80]}"
            for c in response.citations[:5]
        )
        md = md + "\n\n### Sources\n" + refs
    return md


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
@mcp.tool(annotations=_RO)
def ask(question: str) -> str:
    """Answer any natural-language question from the knowledge base.

    Default tool for chat. Pass the user's message unchanged. Searches project
    context (code, APIs, architecture, defects, incidents). Examples: "how does
    eligibility work?", "review DE194624",
    "what depends on order-service?".
    """
    key = _cache_key("ask", question=question)
    hit = _cache.get(key)
    if hit is not None:
        return hit
    result = _emit(_service().ask(question))
    _cache.set(key, result)
    return result


@mcp.tool(annotations=_RW)
def rate_answer(interaction_id: str = "last", rating: int = 5, note: str = "") -> str:
    """Rate the last (or a given) ask result 1–5 so retrieval and aliases improve."""
    _cache.invalidate_all()  # rating changes boost weights; stale cached answers invalid
    return _emit(_service().rate_answer(interaction_id, rating, note))


@mcp.tool(annotations=_RO)
def learning_status() -> str:
    """Show aliases, retrieval boosts, and interaction counts the system has learned."""
    return _emit(_service().learning_status())


@mcp.tool(annotations=_RO)
def explain_service(service_name: str) -> str:
    """Explain a microservice: endpoints, entities, tables, events, dependencies."""
    key = _cache_key("explain_service", service_name=service_name)
    hit = _cache.get(key)
    if hit is not None:
        return hit
    result = _emit(_service().explain_service(service_name))
    _cache.set(key, result)
    return result


@mcp.tool(annotations=_RO)
def find_endpoint(api_name: str) -> str:
    """Find REST endpoints by name/path/keyword across all services."""
    key = _cache_key("find_endpoint", api_name=api_name)
    hit = _cache.get(key)
    if hit is not None:
        return hit
    result = _emit(_service().find_endpoint(api_name))
    _cache.set(key, result)
    return result


@mcp.tool(annotations=_RO)
def trace_business_flow(flow_name: str) -> str:
    """Trace an end-to-end business flow across services, endpoints and events."""
    return _emit(_service().trace_business_flow(flow_name))


@mcp.tool(annotations=_RO)
def impact_analysis(entity_or_table: str) -> str:
    """Analyze the blast radius of changing an entity, table or service."""
    return _emit(_service().impact_analysis(entity_or_table))


@mcp.tool(annotations=_RO)
def analyze_defect(problem_statement: str) -> str:
    """Root-cause a defect: likely services, code paths and remediation."""
    return _emit(_service().analyze_defect(problem_statement))


@mcp.tool(annotations=_RO)
def implement_feature(requirement: str) -> str:
    """Produce an implementation plan (services, APIs, DTOs, tables, events)."""
    return _emit(_service().implement_feature(requirement))


@mcp.tool(annotations=_RO)
def search_domain_knowledge(query: str) -> str:
    """Semantic + graph search across code, docs, architecture and incidents."""
    key = _cache_key("search_domain_knowledge", query=query)
    hit = _cache.get(key)
    if hit is not None:
        return hit
    result = _emit(_service().search_domain_knowledge(query))
    _cache.set(key, result)
    return result


@mcp.tool(annotations=_RO)
def generate_architecture_summary() -> str:
    """Summarize the whole ecosystem with a Mermaid service-dependency graph."""
    key = _cache_key("generate_architecture_summary")
    hit = _cache.get(key)
    if hit is not None:
        return hit
    result = _emit(_service().generate_architecture_summary())
    _cache.set(key, result)
    return result


@mcp.tool(annotations=_RO)
def inspect_application() -> str:
    """Inspection bible for the whole platform: security, quality, coverage, schema, events, architecture scores."""
    return _emit(_service().inspect_application())


@mcp.tool(annotations=_RO)
def inspect_service(service_name: str) -> str:
    """Inspection bible scorecard for one microservice (extractive, graph-backed)."""
    return _emit(_service().inspect_service(service_name))


@mcp.tool(annotations=_RO)
def explain_business_capability(query: str) -> str:
    """Discover and explain business capabilities mapped to services, APIs, databases, and events."""
    return _emit(_service().explain_business_capability(query))


@mcp.tool(annotations=_RO)
def service_dependents(service_name: str) -> str:
    """Traversal query: what depends on Service X (direct and transitive)."""
    return _emit(_service().service_dependents(service_name))


@mcp.tool(annotations=_RO)
def service_failure_impact(service_name: str) -> str:
    """Traversal query: what can be impacted if Service Y fails."""
    return _emit(_service().service_failure_impact(service_name))


@mcp.tool(annotations=_RO)
def onboarding_assistant(topic: str) -> str:
    """Generate a developer onboarding path and reading list for a topic."""
    return _emit(_service().onboarding_assistant(topic))


@mcp.tool(annotations=_RO)
def get_api_contract(endpoint_path: str) -> str:
    """Full API contract: request/response JSON schemas, auth, validation rules, error codes."""
    return _emit(_service().get_api_contract(endpoint_path))


@mcp.tool(annotations=_RO)
def get_execution_flow(endpoint_path: str) -> str:
    """Get execution/business/technical flow knowledge for an API endpoint path."""
    return _emit(_service().get_execution_flow(endpoint_path))


@mcp.tool(annotations=_RO)
def compare_branches(repo_name: str, branch1: str, branch2: str) -> str:
    """Compare two git branches: changed APIs, impacted services, risk summary."""
    return _emit(_service().compare_branches(repo_name, branch1, branch2))


@mcp.tool(annotations=_RO)
def full_kt(service_name: str) -> str:
    """Complete knowledge transfer: all APIs with contracts, design patterns, service flows, config, events, DTOs."""
    return _emit(_service().full_kt(service_name))


@mcp.tool(annotations=_RO)
def security_audit(service_name: str) -> str:
    """Security audit: unprotected endpoints, hardcoded secrets, missing @Valid, exception handler gaps."""
    return _emit(_service().security_audit(service_name))


@mcp.tool(annotations=_RO)
def dependency_report(service_name: str) -> str:
    """Maven dependency tree with CVE flags, Spring Boot version, and scope breakdown."""
    return _emit(_service().dependency_report(service_name))


@mcp.tool(annotations=_RO)
def schema_analysis(service_name: str) -> str:
    """JPA entity schema: columns, relationships, N+1 risks, missing indexes, migration context."""
    return _emit(_service().schema_analysis(service_name))


@mcp.tool(annotations=_RO)
def find_antipatterns(service_name: str) -> str:
    """Detect code smells: God classes, field injection, missing @Transactional, N+1, hardcoded values."""
    return _emit(_service().find_antipatterns(service_name))


@mcp.tool(annotations=_RO)
def kafka_topology() -> str:
    """Full Kafka event topology: producer → topic → consumer flows, unmatched events, Mermaid diagram."""
    return _emit(_service().kafka_topology())


@mcp.tool(annotations=_RO)
def generate_openapi(service_name: str) -> str:
    """Generate an OpenAPI 3.0 YAML specification from the knowledge graph."""
    return _emit(_service().generate_openapi(service_name))


@mcp.tool(annotations=_RO)
def test_coverage_map(service_name: str) -> str:
    """Map test coverage: which classes have tests, untested endpoints/services, test type breakdown."""
    return _emit(_service().test_coverage_map(service_name))


@mcp.tool(annotations=_RO)
def get_method_detail(class_name: str, method_name: str) -> str:
    """Full method body, parameters, local variables, call chain — target specific logic for fixes."""
    return _emit(_service().get_method_detail(class_name, method_name))


@mcp.tool(annotations=_RO)
def analyze_user_story(story_id_or_description: str) -> str:
    """Fetch a Rally user story (or plain description) and map it to affected services, classes and methods."""
    return _emit(_service().analyze_user_story(story_id_or_description))


@mcp.tool(annotations=_RO)
def cross_service_impact(change_description: str) -> str:
    """Given a change description, identify direct and transitive impact across ALL microservices."""
    return _emit(_service().cross_service_impact(change_description))


@mcp.tool(annotations=_RO)
def explain_code(file_path: str, start_line: int | None = None,
                 end_line: int | None = None) -> str:
    """Instantly explain code in a file at given line range using the pre-built knowledge graph."""
    return _emit(_service().explain_code(file_path, start_line, end_line))


@mcp.tool(annotations=_RO)
def trace_call_chain(class_name: str, method_name: str) -> str:
    """Full execution trace: HTTP entry → method body → outbound calls → database layer."""
    return _emit(_service().trace_call_chain(class_name, method_name))


@mcp.tool(annotations=_RO)
def find_similar_code(description: str, service_name: str | None = None) -> str:
    """Find all code across services implementing similar logic to the description."""
    return _emit(_service().find_similar_code(description, service_name))


@mcp.tool(annotations=_RO)
def explain_exception_flow(exception_name: str) -> str:
    """Where is this exception thrown, how it propagates, and what HTTP status it maps to."""
    return _emit(_service().explain_exception_flow(exception_name))


@mcp.tool(annotations=_RO)
def list_indexed_files(service_name: str | None = None) -> str:
    """List all files in the knowledge base — use this to discover what can be explained."""
    return _emit(_service().list_indexed_files(service_name))


@mcp.tool(annotations=_RO)
def get_defect_changes(ticket_id: str, file_filter: str = "java") -> str:
    """Use this whenever a user asks to review a defect, story, or ticket (e.g. 'review DE194624',
    'what changed in US645138', 'show me the fix for DE888'). Returns a complete ready-to-display
    Markdown report: commit timeline, before/after code diffs, and affected endpoints/methods.
    file_filter: 'java' (default, shows only Java source diffs — fastest, lowest token cost),
                 'all' (every changed file including config/build files)."""
    return _emit(_service().get_defect_changes(ticket_id, file_filter=file_filter))


# --------------------------------------------------------------------------- #
# Enterprise code-intelligence / RCA tools (additive — see
# docs/ENTERPRISE_ARCHITECTURE.md for the full design). All read-only except
# analyse_exception/classify_incident, which persist a first-class incident.
# --------------------------------------------------------------------------- #
@mcp.tool(annotations=_RO)
def find_callers(class_name: str, method_name: str) -> str:
    """Find all callers (endpoints, service methods, repositories) of a method,
    e.g. 'Find all callers of processPayment()'."""
    return _emit(_service().find_callers(class_name, method_name))


@mcp.tool(annotations=_RO)
def find_callees(class_name: str, method_name: str) -> str:
    """Find everything a method calls out to — methods, repositories, thrown exceptions."""
    return _emit(_service().find_callees(class_name, method_name))


@mcp.tool(annotations=_RO)
def call_graph(class_name: str, method_name: str,
              direction: str = "both") -> str:
    """P4.3: unified caller + callee graph for a method.

    direction: 'callers' (who calls this), 'callees' (what it calls),
               'both' (default — full neighbourhood).
    """
    svc = _service()
    if direction == "callers":
        return _emit(svc.find_callers(class_name, method_name))
    if direction == "callees":
        return _emit(svc.find_callees(class_name, method_name))
    # both: merge callers + callees markdown
    callers = svc.find_callers(class_name, method_name)
    callees = svc.find_callees(class_name, method_name)
    from .models import ToolResponse
    merged = ToolResponse(
        tool="call_graph",
        query={"class": class_name, "method": method_name, "direction": direction},
        summary=f"{class_name}.{method_name}: callers + callees",
        markdown=(callers.markdown or callers.summary)
                 + "\n\n---\n\n"
                 + (callees.markdown or callees.summary),
    )
    return _emit(merged)


@mcp.tool(annotations=_RO)
def trace_execution_path(source: str, target: str) -> str:
    """Trace the call/dependency path between two named entities (endpoint, class,
    method, or service), e.g. 'trace execution path from OrderController to OrderRepository'."""
    return _emit(_service().trace_execution_path(source, target))


@mcp.tool(annotations=_RO)
def find_api_path(source_service: str, target_service: str) -> str:
    """Find the service-to-service call path connecting two services, e.g.
    'Which APIs call InventoryService?' (pass the caller as source_service)."""
    return _emit(_service().find_api_path(source_service, target_service))


@mcp.tool(annotations=_RO)
def dependency_analysis(service_name: str) -> str:
    """Library dependencies + inter-service dependency graph for a service."""
    return _emit(_service().dependency_analysis(service_name))


@mcp.tool(annotations=_RO)
def search_code(query: str, service_name: str | None = None) -> str:
    """Search source code using the multi-stage retrieval pipeline (intent detection,
    graph + vector retrieval, reranking, and an explicit answer-verification/
    hallucination check). Prefer this over search_domain_knowledge when the question
    is specifically about code (e.g. 'Where is customer validation performed?')."""
    return _emit(_service().search_code(query, service_name))


@mcp.tool(annotations=_RO)
def explain_architecture() -> str:
    """Whole-ecosystem architecture summary with a Mermaid dependency graph."""
    return _emit(_service().explain_architecture())


@mcp.tool(annotations=_RW)
def analyse_exception(
    stack_trace: str | None = None, exception_type: str | None = None,
    message: str | None = None, service_name: str | None = None,
    version: str | None = None, environment: str | None = None,
    logs: str | None = None,
    customer_impact: str | None = None, revenue_impact: str | None = None,
    frequency_per_hour: float | None = None, services_affected: int | None = None,
    recovery_time_minutes: float | None = None,
) -> str:
    """Root-cause a production exception from a stack trace (+ optional logs, service,
    version, and hybrid severity inputs). Returns probable root cause, confidence,
    severity, affected services, suggested fix, related code areas, and related
    historical incidents. Persists the result as a first-class incident."""
    return _emit(_service().analyse_exception(
        stack_trace=stack_trace, exception_type=exception_type, message=message,
        service_name=service_name, version=version, environment=environment,
        logs=logs,
        customer_impact=customer_impact, revenue_impact=revenue_impact,
        frequency_per_hour=frequency_per_hour, services_affected=services_affected,
        recovery_time_minutes=recovery_time_minutes,
    ))


@mcp.tool(annotations=_RW)
def classify_incident(
    description: str, service_name: str | None = None,
    customer_impact: str | None = None, revenue_impact: str | None = None,
    frequency_per_hour: float | None = None, services_affected: int | None = None,
    recovery_time_minutes: float | None = None,
    historical_pattern_similarity: float | None = None,
) -> str:
    """Classify an incident P1-P4 using explainable, rule-based hybrid severity
    scoring (customer/revenue impact, frequency, blast radius, recovery time,
    historical pattern match) — never LLM-only. Persists the incident."""
    return _emit(_service().classify_incident(
        description, service_name=service_name, customer_impact=customer_impact,
        revenue_impact=revenue_impact, frequency_per_hour=frequency_per_hour,
        services_affected=services_affected, recovery_time_minutes=recovery_time_minutes,
        historical_pattern_similarity=historical_pattern_similarity,
    ))


@mcp.tool(annotations=_RO)
def find_related_incidents(query: str) -> str:
    """Retrieve previously-resolved incidents similar to a query (exception type,
    symptom description, or free text) — the RCA 'knowledge memory'."""
    return _emit(_service().find_related_incidents(query))


@mcp.tool(annotations=_RO)
def compare_versions(repo_name: str, version_a: str, version_b: str) -> str:
    """Compare two versions of a repo — release tags, branches, or commit SHAs
    (e.g. 'compare release 2.7 and 2.8'). Reports changed files and impacted
    endpoints/services."""
    return _emit(_service().compare_versions(repo_name, version_a, version_b))


# --------------------------------------------------------------------------- #
# Resources (read-only, addressable context the client can attach directly)
# --------------------------------------------------------------------------- #
@mcp.resource("kb://architecture/summary")
def architecture_summary_resource() -> str:
    """Whole-ecosystem architecture summary as Markdown (incl. Mermaid graph)."""
    return _service().generate_architecture_summary().markdown


@mcp.resource("kb://services")
def services_resource() -> str:
    """JSON list of indexed services with node/edge statistics."""
    svc = _service()
    return json.dumps(
        {"services": svc.graph.services(), "stats": svc.graph.stats()},
        indent=2, default=str,
    )


@mcp.resource("kb://service/{service_name}")
def service_resource(service_name: str) -> str:
    """Structured JSON profile for a single service."""
    return json.dumps(_service().explain_service(service_name).as_dict(),
                      indent=2, default=str)


# --------------------------------------------------------------------------- #
# Prompts (slash-menu workflows — no extra arguments)
#
# Clients such as Open WebUI treat every prompt parameter as a required
# form field. These prompts therefore take no arguments: they instruct the
# model to use the user's current chat message and call `ask`.
# --------------------------------------------------------------------------- #
_PROMPT_USE_CHAT = (
    "The request is the user's current chat message. "
    "Do not ask them to fill extra prompt fields or retype the question.\n"
    "Call `ask` with that full message as `question`. "
    "Call a specialized MCP tool afterwards only if `ask` is incomplete."
)


@mcp.prompt
def chat() -> str:
    """Answer the current message from the knowledge base (no extra text)."""
    return (
        "You are the engineering knowledge assistant.\n"
        f"{_PROMPT_USE_CHAT}\n"
        "Cite sources from the tool result. If evidence is incomplete, say so. "
        "Do not invent service names, APIs, tables, topics, or root causes."
    )


@mcp.prompt
def inspect_platform() -> str:
    """Run the inspection bible on the current message (app or named service)."""
    return (
        "The user wants an inspection scorecard.\n"
        f"{_PROMPT_USE_CHAT}\n"
        "Prefer `inspect_application` for the whole platform or `inspect_service` "
        "when a service is named. Do not invent scores; use the tool numbers."
    )


@mcp.prompt
def default_platform_assistant() -> str:
    """Default assistant policy — uses the current message, no extra fields."""
    return (
        "You are the default engineering assistant for this microservice platform.\n"
        f"{_PROMPT_USE_CHAT}\n"
        "Prefer graph-backed answers with cited evidence. "
        "State uncertainty when evidence is incomplete."
    )


@mcp.prompt
def investigate_defect() -> str:
    """Investigate a production defect described in the current message."""
    return (
        "The user's message is a production defect or incident.\n"
        f"{_PROMPT_USE_CHAT}\n"
        "If `ask` is not enough, follow up with `analyze_defect`, then "
        "`explain_service` / `impact_analysis` on suspects. "
        "Summarize root cause, affected services, and remediation with sources."
    )


@mcp.prompt
def plan_feature() -> str:
    """Plan a feature described in the current message."""
    return (
        "The user's message is a feature/requirement to implement.\n"
        f"{_PROMPT_USE_CHAT}\n"
        "Optional follow-ups: `implement_feature`, `trace_business_flow`, "
        "`find_endpoint`. Produce a step-by-step plan with endpoints, DTOs, "
        "entities, tables, events, risks, and rollout order."
    )


@mcp.prompt
def onboard_new_developer() -> str:
    """Onboarding / knowledge transfer for the service named in the message."""
    return (
        "The user's message names the service or topic they are joining.\n"
        f"{_PROMPT_USE_CHAT}\n"
        "Optional follow-up: `full_kt` if a service name is known. "
        "Include APIs, patterns, entities, events, dependencies, a reading "
        "order, and a few starter tasks."
    )


@mcp.prompt
def document_api() -> str:
    """Document the API/endpoint mentioned in the current message."""
    return (
        "The user's message identifies an API or path to document.\n"
        f"{_PROMPT_USE_CHAT}\n"
        "Optional follow-ups: `get_api_contract`, `find_endpoint`. "
        "Cover method, path, auth, request/response examples, errors, and call chain."
    )


@mcp.prompt
def review_branch_changes() -> str:
    """Review branch/version changes described in the current message."""
    return (
        "The user's message names the repo and the two branches or versions.\n"
        f"{_PROMPT_USE_CHAT}\n"
        "Optional follow-up: `compare_branches` or `compare_versions` when "
        "repo and refs are known. Rank risk: breaking changes, new endpoints, review checklist."
    )


@mcp.prompt
def architect_review() -> str:
    """Senior architect review of the service named in the current message."""
    return (
        "The user's message names the service to review.\n"
        f"{_PROMPT_USE_CHAT}\n"
        "Optional follow-ups: `security_audit`, `dependency_report`, "
        "`schema_analysis`, `find_antipatterns`, `test_coverage_map`. "
        "End with risk score and ranked action items."
    )


@mcp.prompt
def generate_api_docs() -> str:
    """Generate API docs for the service named in the current message."""
    return (
        "The user's message names the service to document.\n"
        f"{_PROMPT_USE_CHAT}\n"
        "Optional follow-ups: `generate_openapi`, `full_kt`. "
        "Produce a Markdown API reference plus OpenAPI YAML if available."
    )


@mcp.prompt
def fix_method_logic() -> str:
    """Fix the method/issue described in the current message."""
    return (
        "The user's message names a class/method and the issue.\n"
        f"{_PROMPT_USE_CHAT}\n"
        "Optional follow-ups: `get_method_detail`, `find_antipatterns`, "
        "`impact_analysis`. Return fixed code, rationale, and tests to update."
    )


@mcp.prompt
def implement_user_story() -> str:
    """Implementation plan for the story in the current message."""
    return (
        "The user's message is a user story id or description.\n"
        f"{_PROMPT_USE_CHAT}\n"
        "Optional follow-ups: `analyze_user_story`, `cross_service_impact`. "
        "Produce a plan with classes, methods, DB/API/event changes, and tests."
    )


@mcp.prompt
def sprint_planning() -> str:
    """Sprint impact map from the current message."""
    return (
        "The user's message describes the sprint or its stories.\n"
        f"{_PROMPT_USE_CHAT}\n"
        "Optional follow-ups: `cross_service_impact`, `kafka_topology`. "
        "Produce a service-impact table, deployment order, and test strategy."
    )


@mcp.prompt
def explain_this_code() -> str:
    """Explain the file/code the user is asking about."""
    return (
        "The user's message (or attached code) is what to explain.\n"
        f"{_PROMPT_USE_CHAT}\n"
        "Optional follow-ups: `explain_code`, `trace_call_chain`, `get_api_contract`."
    )


@mcp.prompt
def code_review() -> str:
    """Code review for the file or change in the current message."""
    return (
        "The user's message names the file or change to review.\n"
        f"{_PROMPT_USE_CHAT}\n"
        "Optional follow-ups: `explain_code`, `find_antipatterns`, "
        "`security_audit`, `test_coverage_map`."
    )


@mcp.prompt
def review_defect() -> str:
    """Show the code changes for the ticket in the current message."""
    return (
        "The user's message contains a defect/story/ticket id (e.g. DE194624).\n"
        f"{_PROMPT_USE_CHAT}\n"
        "If `ask` returns ```diff blocks, render them exactly as-is. "
        "Do not paraphrase diffs."
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def _force_binary_stdio() -> None:
    """Prevent Windows text-mode CRLF translation from corrupting JSON-RPC.

    On Windows the console file descriptors translate ``\\n`` <-> ``\\r\\n``,
    which mangles the newline-delimited JSON-RPC framing on stdio and shows up
    as spurious ``Invalid JSON ... '\\n'`` errors on the client. Force the raw
    fds to binary so bytes pass through untouched.
    """
    import sys

    if sys.platform != "win32":
        return
    import msvcrt
    import os

    for stream in (sys.stdin, sys.stdout):
        try:
            msvcrt.setmode(stream.fileno(), os.O_BINARY)
        except (OSError, ValueError):
            pass


def _redirect_stdout_to_stderr() -> None:
    """Silence stray print() calls from ML libraries (fastembed, onnxruntime, etc.).

    In stdio mode the MCP transport reads sys.stdout.buffer directly, so we
    must keep that buffer pointing at fd 1.  We replace the text-layer
    sys.stdout with a wrapper whose write() goes to stderr while its .buffer
    property still returns the real stdout buffer for the transport.
    """
    import io
    import sys

    _real_buffer = sys.stdout.buffer

    class _StdioProxy(io.RawIOBase):
        """Forwards text writes to stderr; exposes real stdout buffer for MCP."""
        def write(self, b):
            """Send raw bytes to the real stderr buffer."""
            return sys.stderr.buffer.write(b)
        def readable(self):
            """Always False — this proxy is write-only."""
            return False
        def writable(self):
            """Always True — this proxy is write-only."""
            return True

    proxy_buffer = io.BufferedWriter(_StdioProxy())

    class _StdoutProxy(io.TextIOWrapper):
        @property
        def buffer(self):          # MCP transport accesses this for raw writes
            """Return the real stdout buffer (bypassing the stderr-forwarding proxy)."""
            return _real_buffer

    sys.stdout = _StdoutProxy(proxy_buffer, encoding="utf-8", line_buffering=True)


def main() -> None:
    """Entrypoint: configure logging/observability and run the MCP server over the configured transport."""
    from starlette.middleware import Middleware
    from starlette.middleware.cors import CORSMiddleware

    settings = get_settings()
    transport = settings.transport.lower()

    # P5.3: CORS hardening — warn loudly if wildcard origins used outside local dev
    if transport == "http":
        _origins = settings.cors_origins_list
        _is_wildcard = _origins == ["*"] or _origins == [b"*"]
        if _is_wildcard and settings.env not in ("local", "dev", "development"):
            log.error(
                "cors_wildcard_in_production",
                env=settings.env,
                message="CORS_ALLOWED_ORIGINS='*' is insecure in non-local environments. "
                        "Set CORS_ALLOWED_ORIGINS to explicit allowed origins.",
            )
            import sys
            print(
                "SECURITY ERROR: CORS_ALLOWED_ORIGINS='*' is not allowed in "
                f"env={settings.env!r}. Set an explicit origin list and restart.",
                file=sys.stderr,
            )
            sys.exit(1)

    log.info("mcp_server_start", transport=transport, env=settings.env, version="1.0.0")

    if transport == "http":
        from .learning.scheduler import start_background_jobs
        start_background_jobs(settings)

        cors_middleware = Middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins_list,
            allow_credentials=settings.cors_allow_credentials,
            allow_methods=settings.cors_methods_list,
            allow_headers=settings.cors_headers_list,
            expose_headers=["Mcp-Session-Id"],
        )
        log.info(
            "http_cors_configured",
            origins=settings.cors_origins_list,
            credentials=settings.cors_allow_credentials,
            host=settings.http_host,
            port=settings.http_port,
        )
        mcp.run(
            transport="http",
            host=settings.http_host,
            port=settings.http_port,
            show_banner=False,
            middleware=[cors_middleware],
        )
    else:
        _force_binary_stdio()
        _redirect_stdout_to_stderr()  # must be after _force_binary_stdio sets binary mode
        # background jobs may log/print; only safe once stdout is redirected off
        # the stdio MCP transport channel.
        from .learning.scheduler import start_background_jobs
        start_background_jobs(settings)
        _prewarm()  # load embedder + graph before first client request
        mcp.run(show_banner=False)  # stdio


if __name__ == "__main__":
    main()
