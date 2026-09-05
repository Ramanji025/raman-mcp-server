"""Module-level helper functions shared by several KnowledgeService mixins."""
from __future__ import annotations

from ...logging import get_logger

log = get_logger(__name__)



def _intent_collections(route) -> tuple[str, ...] | None:
    """P2.3: return scoped collections for the route intent, or None for all."""
    if route is None:
        return None
    from ..query_router import collections_for_intent
    return collections_for_intent(route.intent)


def _intent_graph_hops(intent: str, base: int) -> int:
    """P2.2: return graph hops for the intent (deeper for cross-service intents)."""
    from ..query_router import graph_hops_for_intent
    return graph_hops_for_intent(intent, base)


def _infer_method_purpose(name: str, called: list[str],
                          throws: list[str], transactional: bool) -> str:
    """Heuristic one-line purpose from method name + call pattern."""
    n = name.lower()
    if n.startswith(("get", "find", "fetch", "load", "retrieve", "read", "query", "list")):
        return f"Read operation — retrieves data{'; @Transactional read' if transactional else ''}"
    if n.startswith(("create", "save", "add", "insert", "persist", "register", "submit")):
        return f"Write operation — creates/persists a record{'; @Transactional' if transactional else ' (check @Transactional)'}"
    if n.startswith(("update", "modify", "patch", "change", "set", "edit")):
        return f"Update operation — modifies existing data{'; @Transactional' if transactional else ' (check @Transactional)'}"
    if n.startswith(("delete", "remove", "cancel", "deactivate", "disable")):
        return f"Delete/deactivate operation{'; @Transactional' if transactional else ' (check @Transactional)'}"
    if n.startswith(("validate", "check", "verify", "assert")):
        return f"Validation — checks input or business rule; throws {throws[0] if throws else 'exception'} on failure"
    if n.startswith(("publish", "send", "emit", "notify", "dispatch", "produce")):
        return "Event publishing — sends message/event to Kafka/queue"
    if n.startswith(("process", "execute", "handle", "run", "perform")):
        repo_calls = [c for c in called if "repository" in c.lower() or "repo" in c.lower()]
        return (f"Business logic orchestration — coordinates "
                f"{len(repo_calls)} DB call(s) and {len(called)} total call(s)")
    kafka_calls = [c for c in called if "kafka" in c.lower() or "template" in c.lower()]
    if kafka_calls:
        return "Produces Kafka event as part of business flow"
    return f"Business method — {len(called)} internal call(s)"


