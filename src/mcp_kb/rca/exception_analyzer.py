"""Production exception analysis: stack-trace parsing + graph correlation.

This module turns a raw stack trace (plus optional logs / service / version)
into a structured :class:`~mcp_kb.models.RCAResult` with a probable root
cause, confidence score, affected services, suggested fix, and pointers back
into the indexed source code and historical incidents.

Design goals (mirrors the "no LLM-only reasoning" requirement used for
severity scoring): the *structural* facts — which frames belong to project
code, which class/method they resolve to in the knowledge graph, whether the
exception type has been seen before — are computed deterministically here.
An LLM (via ``agents/multi_stage_workflow.py``) may be layered on top to turn
these facts into prose, but the facts themselves, and the confidence score,
do not depend on it.
"""
from __future__ import annotations

import re
import uuid
from typing import Any, Protocol

from ..graph.base import GraphStorePort
from ..logging import get_logger
from ..models import ExceptionEvent, RCAResult, SeverityScore, StackFrame

log = get_logger(__name__)

# Matches "\tat com.example.Foo.bar(Foo.java:42)" and the "Foo.java:42" part
# of Kotlin/inline-lambda frames too (best-effort, tuned for Java/Spring).
_FRAME_RE = re.compile(
    r"at\s+(?P<fqcn>[\w$.]+)\.(?P<method>[\w$<>]+)\((?P<file>[\w$]+\.\w+):(?P<line>\d+)\)"
)
# "com.foo.BarException: message" or "Caused by: com.foo.BarException: message"
_HEADER_RE = re.compile(
    r"^(?:Caused by:\s*)?(?P<type>[\w$.]+(?:Exception|Error|Throwable))\s*(?::\s*(?P<message>.*))?$"
)

_ENV_RE = re.compile(r"\b(prod|production|stage|staging|qa|uat|dev|development|test)\b",
                     re.IGNORECASE)
_VERSION_RE = re.compile(r"\b(?:version|ver|release)\s*[:=]?\s*([A-Za-z0-9_.\-]+)",
                         re.IGNORECASE)
_SERVICE_RE = re.compile(r"\b([a-z0-9][a-z0-9\-_.]*(?:service|gateway|api))\b",
                         re.IGNORECASE)

# Frames inside these packages are framework/JDK noise, not project code —
# used only to rank candidate root-cause frames, never to hide information.
_FRAMEWORK_PACKAGE_PREFIXES = (
    "java.", "javax.", "sun.", "jdk.", "org.springframework.", "org.hibernate.",
    "org.apache.", "com.zaxxer.hikari.", "reactor.", "io.netty.", "kotlin.",
    "org.junit.", "net.sf.cglib.", "org.aspectj.", "$Proxy",
)


def _is_framework_frame(fqcn: str) -> bool:
    return any(fqcn.startswith(p) or p in fqcn for p in _FRAMEWORK_PACKAGE_PREFIXES)


class ExceptionBlock:
    """One exception in a ``Caused by:`` chain, with its own frames."""

    __slots__ = ("exception_type", "frames", "message")

    def __init__(self, exception_type: str, message: str | None) -> None:
        self.exception_type = exception_type
        self.message = message
        self.frames: list[StackFrame] = []

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"ExceptionBlock({self.exception_type!r}, frames={len(self.frames)})"


def parse_stack_trace_blocks(text: str) -> list[ExceptionBlock]:
    """Parse a stack trace preserving ``Caused by:`` nesting.

    Each returned block owns only the frames printed underneath its header,
    so the deepest (last, "original cause") block's *own* frames — not the
    outer exception's frames — are used to localise the true root cause.
    """
    blocks: list[ExceptionBlock] = []
    current: ExceptionBlock | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        header_match = _HEADER_RE.match(line)
        if header_match:
            msg = header_match.group("message")
            current = ExceptionBlock(header_match.group("type"), msg.strip() if msg else None)
            blocks.append(current)
            continue
        frame_match = _FRAME_RE.search(line)
        if frame_match and current is not None:
            fqcn = frame_match.group("fqcn")
            package = fqcn.rsplit(".", 1)[0] if "." in fqcn else ""
            class_name = fqcn.rsplit(".", 1)[-1]
            current.frames.append(StackFrame(
                class_name=class_name, method_name=frame_match.group("method"),
                file_name=frame_match.group("file"), line_number=int(frame_match.group("line")),
                package=package, is_project_code=not _is_framework_frame(fqcn),
            ))
    return blocks


def parse_stack_trace(text: str) -> tuple[list[str], str | None, list[StackFrame]]:
    """Parse a Java-style stack trace into (exception_type_chain, message, frames).

    ``exception_type_chain`` lists every exception type encountered, in the
    order printed (outermost first, ``Caused by:`` entries after). The last
    entry is conventionally the deepest/original cause. This is a flattened
    convenience view over :func:`parse_stack_trace_blocks` — prefer that
    function when root-cause frame attribution matters (it keeps each
    exception's frames correctly scoped to its own ``Caused by:`` section).
    """
    blocks = parse_stack_trace_blocks(text)
    chain = [b.exception_type for b in blocks]
    # Prefer the deepest ("Caused by:") block's message, consistent with
    # chain[-1] being the deepest/original cause; fall back to any message.
    message = next((b.message for b in reversed(blocks) if b.message), None)
    frames = [f for b in blocks for f in b.frames]
    return chain, message, frames


def infer_environment(text: str | None) -> str | None:
    """Guess the deployment environment (production/staging/qa/development) from log text."""
    if not text:
        return None
    m = _ENV_RE.search(text)
    if not m:
        return None
    token = m.group(1).lower()
    if token.startswith("prod"):
        return "production"
    if token.startswith("stag"):
        return "staging"
    if token in ("qa", "uat"):
        return token
    if token.startswith("dev"):
        return "development"
    return token


def infer_version(text: str | None) -> str | None:
    """Extract a version string from log text, if present."""
    if not text:
        return None
    m = _VERSION_RE.search(text)
    return m.group(1) if m else None


def infer_service_name(text: str | None) -> str | None:
    """Extract a likely service name from log text, if present."""
    if not text:
        return None
    m = _SERVICE_RE.search(text.lower())
    return m.group(1) if m else None


# --------------------------------------------------------------------------- #
# Suggested-fix heuristics — keyword-driven, deterministic, auditable.
# --------------------------------------------------------------------------- #
_FIX_TEMPLATES: list[tuple[str, str]] = [
    ("NullPointerException",
     "Add a null-check / Optional guard before the dereference at the flagged "
     "line, and validate the upstream call that supplies the value."),
    ("SQLException",
     "Check the query/connection at the flagged repository method — likely a "
     "constraint violation, connection-pool exhaustion, or malformed SQL. "
     "Verify Hikari pool sizing and DB health."),
    ("TimeoutException",
     "The downstream call exceeded its SLA — check the callee's health/latency, "
     "and confirm Resilience4j @Retry/@CircuitBreaker/timeout config on the caller."),
    ("ConnectException",
     "The target host/service was unreachable — check service discovery, "
     "network policy, and whether the dependency was down or still starting."),
    ("IllegalArgumentException",
     "An upstream caller passed an invalid value — tighten @Valid/@NotNull "
     "input validation at the entry-point controller/DTO."),
    ("ValidationException",
     "A business-rule validation failed — check the validator referenced in "
     "the stack for the specific rule and whether upstream input changed shape."),
    ("OutOfMemoryError",
     "Investigate heap usage/leak — check for unbounded collections or large "
     "result sets loaded per request; consider pagination and heap dump analysis."),
    ("DataIntegrityViolationException",
     "A DB constraint (unique/foreign key/not-null) was violated — check the "
     "entity mapping and the caller's input against the schema."),
    ("OptimisticLockingFailureException",
     "Concurrent updates raced on the same row — add retry-with-backoff or "
     "review the transaction boundary/isolation level."),
    ("FeignException",
     "A downstream service call failed — check the callee's availability and "
     "the Feign client's error decoder / fallback."),
]


def _suggest_fix(exception_type: str, frame: StackFrame | None) -> str:
    for keyword, template in _FIX_TEMPLATES:
        if keyword in exception_type:
            loc = f" (see {frame.class_name}.{frame.method_name}, {frame.file_name}:{frame.line_number})" \
                if frame else ""
            return template + loc
    loc = f" Start at {frame.class_name}.{frame.method_name} ({frame.file_name}:{frame.line_number})." \
        if frame else ""
    return ("Review the failure condition at the root-cause frame and add a "
            "regression test covering it." + loc)


class IncidentSimilaritySource(Protocol):
    """Anything that can retrieve incidents similar to a query (duck-typed)."""

    def find_similar(self, query: str, *, top_k: int = 5) -> list[dict[str, Any]]:
        """Return incidents similar to `query`, most relevant first."""
        ...


def _root_cause_frame(frames: list[StackFrame]) -> StackFrame | None:
    """First project-owned frame — where the deepest cause actually surfaced
    in code we control, skipping framework/JDK noise."""
    for f in frames:
        if f.is_project_code:
            return f
    return frames[0] if frames else None


def correlate_frames_with_graph(frames: list[StackFrame], graph: GraphStorePort) -> list[dict]:
    """Resolve each project-code frame to a node in the knowledge graph."""
    areas: list[dict] = []
    for f in frames:
        if not f.is_project_code:
            continue
        matches = graph.find_by_file_lines(f.file_name or f.class_name, f.line_number, f.line_number) \
            if f.file_name else []
        if not matches:
            matches = graph.find_nodes(f.class_name, limit=3)
        for m in matches[:1]:
            areas.append({
                "class": f.class_name, "method": f.method_name,
                "file": f.file_name, "line": f.line_number,
                "service": m.get("service"), "node_type": m.get("type"),
                "node_id": m.get("id"),
            })
        if not matches:
            areas.append({
                "class": f.class_name, "method": f.method_name,
                "file": f.file_name, "line": f.line_number,
                "service": None, "node_type": None, "node_id": None,
            })
    return areas


def analyze_exception(
    *,
    graph: GraphStorePort,
    stack_trace: str | None = None,
    exception_type: str | None = None,
    message: str | None = None,
    service_name: str | None = None,
    version: str | None = None,
    environment: str | None = None,
    logs: str | None = None,
    incident_source: IncidentSimilaritySource | None = None,
    severity_score: SeverityScore | None = None,
) -> tuple[ExceptionEvent, RCAResult]:
    """Run deterministic RCA over a stack trace / exception report.

    Returns the parsed :class:`ExceptionEvent` alongside the
    :class:`RCAResult`. Callers (the ``analyse_exception`` MCP tool) are
    responsible for persisting the result as an :class:`IncidentRecord` via
    ``rca.incident_store``.
    """
    chain: list[str] = []
    frames: list[StackFrame] = []
    parsed_message = message
    root_block_frames: list[StackFrame] = []
    if stack_trace:
        blocks = parse_stack_trace_blocks(stack_trace)
        chain = [b.exception_type for b in blocks]
        parsed_message_from_trace = next((b.message for b in reversed(blocks) if b.message), None)
        parsed_message = parsed_message or parsed_message_from_trace
        frames = [f for b in blocks for f in b.frames]
        # The deepest ("Caused by:") block is the true origin — its own frames
        # (not the outer exception's) localise where it actually surfaced.
        if blocks:
            root_block_frames = blocks[-1].frames
    root_type = exception_type or (chain[-1] if chain else "UnknownException")

    inferred_service = service_name or infer_service_name("\n".join(filter(None, [stack_trace, logs])))
    inferred_version = version or infer_version("\n".join(filter(None, [stack_trace, logs])))
    inferred_environment = environment or infer_environment("\n".join(filter(None, [stack_trace, logs])))

    root_frame = _root_cause_frame(root_block_frames or frames)
    code_areas = correlate_frames_with_graph(frames, graph)
    affected_services = sorted({a["service"] for a in code_areas if a.get("service")} |
                                ({inferred_service} if inferred_service else set()))

    related_incidents: list[dict[str, Any]] = []
    if incident_source is not None:
        query = f"{root_type} {parsed_message or ''} {inferred_service or ''}".strip()
        try:
            related_incidents = incident_source.find_similar(query, top_k=5)
        except Exception as exc:  # pragma: no cover - defensive, never fail RCA on this
            log.warning("incident_similarity_lookup_failed", error=str(exc))

    confidence = 0.1  # baseline: we at least attempted analysis
    evidence: list[dict[str, Any]] = []
    if chain or exception_type:
        confidence += 0.25
        evidence.append({"signal": "exception_type_identified", "value": root_type})
    if any(a.get("node_id") for a in code_areas):
        confidence += 0.3
        evidence.append({"signal": "stack_frame_resolved_to_indexed_code",
                          "count": sum(1 for a in code_areas if a.get("node_id"))})
    if related_incidents:
        confidence += 0.2
        evidence.append({"signal": "similar_historical_incidents", "count": len(related_incidents)})
    if root_frame is not None:
        confidence += 0.15
        evidence.append({"signal": "root_cause_frame_isolated",
                          "frame": f"{root_frame.class_name}.{root_frame.method_name}"})
    confidence = round(min(confidence, 1.0), 2)

    similar_root_causes = [
        str(i.get("root_cause", "")).strip() for i in related_incidents
        if str(i.get("root_cause", "")).strip()
    ]
    similar_fixes = [
        str(i.get("fix_summary", "")).strip() for i in related_incidents
        if str(i.get("fix_summary", "")).strip()
    ]

    if root_frame is not None:
        root_cause = (
            f"{root_type} raised from `{root_frame.class_name}.{root_frame.method_name}` "
            f"({root_frame.file_name}:{root_frame.line_number})"
            + (f" — {parsed_message}" if parsed_message else "")
        )
    else:
        root_cause = f"{root_type}" + (f" — {parsed_message}" if parsed_message else "") + \
            " (no stack frame available; provide a full stack trace for precise localisation)"

    if similar_root_causes:
        root_cause += (". Similar incidents suggest: " + " | ".join(similar_root_causes[:2]))

    suggested_fix = _suggest_fix(root_type, root_frame)
    if similar_fixes:
        suggested_fix += (" Historical fixes: " + " | ".join(similar_fixes[:2]))

    result = RCAResult(
        probable_root_cause=root_cause,
        confidence=confidence,
        severity=severity_score,
        affected_services=affected_services,
        suggested_fix=suggested_fix,
        related_code_areas=code_areas,
        related_incidents=related_incidents,
        evidence=evidence,
    )
    event = ExceptionEvent(
        id=str(uuid.uuid4()), exception_type=root_type, message=parsed_message,
        service_name=inferred_service, version=inferred_version,
        environment=inferred_environment, stack_frames=frames, logs=logs,
    )
    return event, result
