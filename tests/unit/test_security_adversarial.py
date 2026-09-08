"""Phase 6: adversarial/fuzz tests for MCP-facing security boundaries.

Uses `hypothesis` for property-based fuzzing where useful, plus explicit
adversarial payloads modeled on real attack classes (path traversal, Cypher
write-injection, oversized input) — mirroring codebase-memory-mcp's "23
adversarial JSON-RPC payloads" CI check, adapted to this project's actual
attack surface (tool args → filesystem paths / Cypher queries), not a
generic protocol fuzzer.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given, settings as hyp_settings, strategies as st

from mcp_kb.security.path_safety import UnsafeNameError, safe_join


# --------------------------------------------------------------------------- #
# safe_join: path traversal
# --------------------------------------------------------------------------- #
TRAVERSAL_PAYLOADS = [
    "../../../../etc/passwd",
    "..\\..\\..\\windows\\system32\\config\\sam",
    "..",
    "../secret",
    "a/../../b",
    "/etc/passwd",
    "C:\\Windows\\System32",
    "\x00etc/passwd",
    "....//....//etc/passwd",
    "",
]


@pytest.mark.parametrize("payload", TRAVERSAL_PAYLOADS)
def test_safe_join_rejects_path_traversal_payloads(tmp_path: Path, payload: str):
    with pytest.raises(UnsafeNameError):
        safe_join(tmp_path, payload, suffix=".md")


@given(st.text(alphabet=st.sampled_from(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"), min_size=1, max_size=40))
@hyp_settings(max_examples=100)
def test_safe_join_accepts_alnum_names_and_stays_inside_base(tmp_path_factory, name: str):
    base = tmp_path_factory.mktemp("adr")
    path = safe_join(base, name, suffix=".md")
    assert base.resolve() in path.parents or path.parent == base.resolve()


@given(st.text(min_size=1, max_size=200))
@hyp_settings(max_examples=200)
def test_safe_join_never_escapes_base_dir_for_arbitrary_text(tmp_path_factory, name: str):
    """Property: for ANY string input, safe_join either raises or returns a
    path inside base_dir — it must never silently produce an escaping path."""
    base = tmp_path_factory.mktemp("snap")
    try:
        path = safe_join(base, name, suffix=".json.gz")
    except UnsafeNameError:
        return  # rejection is the expected/safe outcome for unsafe input
    resolved_base = base.resolve()
    assert resolved_base == path.parent, (
        f"safe_join escaped base_dir: name={name!r} -> {path}"
    )


# --------------------------------------------------------------------------- #
# query_graph: Cypher write-clause rejection (adversarial injection attempts)
# --------------------------------------------------------------------------- #
WRITE_INJECTION_PAYLOADS = [
    "MATCH (n) DETACH DELETE n",
    "MATCH (n:KBNode) SET n.pwned = true RETURN n",
    "CREATE (n:Evil {owned: true}) RETURN n",
    "MATCH (n) WHERE true WITH n CALL db.dropIndex('x') RETURN n",
    "  match (n) delete n  ",  # case-insensitivity check
    "MATCH (n) MERGE (m:Evil) RETURN m",
    "LOAD CSV FROM 'file:///etc/passwd' AS line RETURN line",
]


class _FakeSession:
    def run(self, *args, **kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("a write query reached the Neo4j session — guard failed")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeDriver:
    def session(self, database=None):
        return _FakeSession()


@pytest.mark.parametrize("payload", WRITE_INJECTION_PAYLOADS)
def test_run_cypher_rejects_write_clauses(monkeypatch, payload: str):
    from mcp_kb.graph.neo4j_store import Neo4jGraphStore

    store = Neo4jGraphStore.__new__(Neo4jGraphStore)  # bypass __init__ (no real driver needed)
    store._driver = _FakeDriver()
    store._database = "neo4j"

    with pytest.raises(ValueError):
        store.run_cypher(payload)
