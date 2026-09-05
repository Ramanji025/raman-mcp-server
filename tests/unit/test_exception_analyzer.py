"""Unit tests for stack-trace parsing and exception RCA (rca/exception_analyzer.py)."""
from __future__ import annotations

from mcp_kb.rca.exception_analyzer import (
    analyze_exception,
    parse_stack_trace,
    parse_stack_trace_blocks,
)

_TRACE = """
java.lang.NullPointerException: Cannot invoke String.length() because x is null
\tat com.example.orders.service.OrderService.validateOrder(OrderService.java:142)
\tat com.example.orders.controller.OrderController.createOrder(OrderController.java:58)
\tat org.springframework.web.method.support.InvocableHandlerMethod.invoke(InvocableHandlerMethod.java:205)
Caused by: java.lang.IllegalStateException: order missing customer id
\tat com.example.orders.service.OrderValidator.check(OrderValidator.java:33)
"""


class FakeGraph:
    """Minimal GraphStorePort stand-in for RCA correlation tests."""

    def __init__(self, file_to_node: dict[str, dict]):
        self._file_to_node = file_to_node

    def find_by_file_lines(self, file_path, start_line=None, end_line=None):
        node = self._file_to_node.get(file_path)
        return [node] if node else []

    def find_nodes(self, text, limit=3):
        return []


def test_parse_stack_trace_blocks_scopes_frames_to_their_own_cause():
    blocks = parse_stack_trace_blocks(_TRACE)
    assert [b.exception_type for b in blocks] == [
        "java.lang.NullPointerException", "java.lang.IllegalStateException",
    ]
    assert len(blocks[0].frames) == 3
    assert len(blocks[1].frames) == 1
    assert blocks[1].frames[0].class_name == "OrderValidator"


def test_parse_stack_trace_flattened_view():
    chain, message, frames = parse_stack_trace(_TRACE)
    assert chain[-1] == "java.lang.IllegalStateException"
    assert message == "order missing customer id"
    assert len(frames) == 4
    assert frames[0].is_project_code is True
    assert frames[2].is_project_code is False  # spring framework frame


def test_root_cause_attributed_to_deepest_cause_block():
    graph = FakeGraph({
        "OrderValidator.java": {"id": "m1", "type": "Method", "service": "rx-order",
                                 "attributes": {}},
        "OrderService.java": {"id": "m2", "type": "Method", "service": "rx-order",
                               "attributes": {}},
    })
    _, result = analyze_exception(graph=graph, stack_trace=_TRACE, service_name="rx-order")
    assert "OrderValidator.check" in result.probable_root_cause
    assert "order missing customer id" in result.probable_root_cause
    assert result.affected_services == ["rx-order"]
    assert 0.0 < result.confidence <= 1.0


def test_confidence_increases_with_more_signals():
    graph_no_match = FakeGraph({})
    _, weak = analyze_exception(graph=graph_no_match, stack_trace=_TRACE)

    graph_with_match = FakeGraph({
        "OrderValidator.java": {"id": "m1", "type": "Method", "service": "rx-order",
                                 "attributes": {}},
    })
    _, strong = analyze_exception(graph=graph_with_match, stack_trace=_TRACE,
                                  service_name="rx-order")
    assert strong.confidence > weak.confidence


def test_suggested_fix_is_keyword_driven_not_generic_for_known_exceptions():
    graph = FakeGraph({})
    _, result = analyze_exception(
        graph=graph, exception_type="java.sql.SQLException",
        message="connection refused", service_name="rx-order",
    )
    assert "connection" in result.suggested_fix.lower() or "query" in result.suggested_fix.lower()


def test_analyze_exception_without_stack_trace_still_returns_result():
    graph = FakeGraph({})
    event, result = analyze_exception(graph=graph, exception_type="TimeoutException",
                                      service_name="rx-pricing")
    assert event.exception_type == "TimeoutException"
    assert result.probable_root_cause
    assert result.confidence >= 0.1
