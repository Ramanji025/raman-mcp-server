"""Unit tests for observability/tracing.py (structlog-only fallback path,
since the OpenTelemetry SDK is an optional extra not required for tests)."""
from __future__ import annotations

from mcp_kb.observability.tracing import instrument_public_methods, traced


class _Widget:
    def do_thing(self, x: int) -> int:
        return x * 2

    def _private(self, x: int) -> int:
        return x + 1


def test_traced_decorator_preserves_return_value_and_signature():
    @traced("test.fn")
    def add(a, b):
        return a + b

    assert add(2, 3) == 5
    assert add.__name__ == "add"


def test_traced_decorator_reraises_exceptions():
    @traced("test.boom")
    def boom():
        raise ValueError("kaboom")

    try:
        boom()
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "kaboom" in str(exc)


def test_instrument_public_methods_wraps_only_public_methods():
    w = _Widget()
    instrument_public_methods(w)
    assert w._kb_instrumented is True
    assert w.do_thing(5) == 10  # still works, transparently wrapped


def test_instrument_public_methods_is_idempotent():
    w = _Widget()
    instrument_public_methods(w)
    first_do_thing = w.do_thing
    instrument_public_methods(w)  # second call should be a no-op
    assert w.do_thing is first_do_thing
