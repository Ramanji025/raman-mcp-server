"""Wires RBAC checks + audit logging around every public tool method.

Mirrors ``observability/tracing.py::instrument_public_methods`` — applied as
a second, independent instrumentation pass over the same
:class:`~mcp_kb.tools.knowledge_service.KnowledgeService` instance so new
tools get access control and an audit trail automatically, with zero
per-tool boilerplate and zero changes to ``server.py``'s 39 call sites.

Both RBAC and audit logging default to safe, backward-compatible behaviour:
RBAC is disabled by default (every call is allowed), and audit logging
records every call (allowed or denied) without ever blocking it on a
logging failure.
"""
from __future__ import annotations

import functools
import inspect
from typing import Any

from ..config import Settings
from ..logging import get_logger
from ..models import ToolResponse
from .audit_log import AuditLog, get_audit_log
from .rbac import RBACPolicy, get_rbac_policy

log = get_logger(__name__)

# Parameter names, in priority order, used to infer the "repository/service"
# a tool call is scoped to — covers every existing + new tool signature
# (service_name, repo_name, source_service, ...) without requiring each tool
# to declare its scope explicitly.
_REPO_PARAM_NAMES = (
    "service_name", "repo_name", "source_service", "target_service", "entity_or_table",
)


def _infer_repo_scope(bound_args: dict[str, Any]) -> str | None:
    for name in _REPO_PARAM_NAMES:
        value = bound_args.get(name)
        if value:
            return str(value)
    return None


def _denied_response(tool_name: str, query: dict[str, Any], reason: str) -> ToolResponse:
    return ToolResponse(
        tool=tool_name, query=query,
        summary=f"Access denied: {reason}",
        data={"error": "access_denied", "reason": reason},
        markdown=f"**Access denied** for `{tool_name}`: {reason}",
    )


def secured(tool_name: str, policy: RBACPolicy, audit: AuditLog):
    """Decorator: RBAC-check + audit-log one bound tool method call."""

    def decorator(fn):
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):  # pragma: no cover - builtins, etc.
            sig = None

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            bound_args: dict[str, Any] = {}
            if sig is not None:
                try:
                    bound = sig.bind_partial(*args, **kwargs)
                    bound.apply_defaults()
                    bound_args = dict(bound.arguments)
                except TypeError:
                    bound_args = dict(kwargs)
            else:
                bound_args = dict(kwargs)

            repo_scope = _infer_repo_scope(bound_args)
            principal = policy.resolve_principal()
            decision = policy.check(tool_name, repo=repo_scope, principal=principal)

            audit.record(principal=principal, tool=tool_name, query=bound_args,
                        repo_scope=repo_scope, allowed=decision.allowed,
                        denial_reason=None if decision.allowed else decision.reason)

            if not decision.allowed:
                log.warning("rbac_access_denied", tool=tool_name, principal=principal,
                            repo=repo_scope, reason=decision.reason)
                return _denied_response(tool_name, bound_args, decision.reason)

            return fn(*args, **kwargs)

        return wrapper

    return decorator


def instrument_with_security(instance: Any, settings: Settings, *,
                              exclude: set[str] | None = None) -> None:
    """Wrap every public method of ``instance`` with RBAC + audit logging."""
    if getattr(instance, "_kb_security_instrumented", False):
        return
    exclude = exclude or set()
    policy = get_rbac_policy(settings)
    audit = get_audit_log(settings)
    cls = type(instance)
    for attr_name in dir(cls):
        if attr_name.startswith("_") or attr_name in exclude:
            continue
        attr = getattr(cls, attr_name, None)
        if not callable(attr):
            continue
        bound = getattr(instance, attr_name)
        if not callable(bound):
            continue
        setattr(instance, attr_name, secured(attr_name, policy, audit)(bound))
    instance._kb_security_instrumented = True
