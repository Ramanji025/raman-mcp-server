"""Tests for applying structured LLM payloads onto graph nodes."""
from __future__ import annotations

import pytest

from mcp_kb.config import Settings
from mcp_kb.ingestion import llm_enrichment
from mcp_kb.ingestion.llm_enrichment import _apply_payload, _parse_json_object, ollama_generate
from mcp_kb.models import GraphNode, NodeType


class _FakeResponse:
    def __init__(self, status_code: int, *, json_body: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json_body = json_body or {}
        self.text = text

    def json(self):
        return self._json_body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeGraph:
    def __init__(self):
        self.nodes: dict[str, GraphNode] = {}

    def add_node(self, node: GraphNode) -> None:
        self.nodes[node.id] = node


def test_parse_json_object_from_fenced_text():
    payload = _parse_json_object('prefix\n```json\n{"class_summary": "orders API"}\n```\n')
    assert payload["class_summary"] == "orders API"


def test_apply_payload_writes_class_method_param_and_block():
    graph = _FakeGraph()
    owner = GraphNode(
        id="controller:svc:OrderController", type=NodeType.CONTROLLER,
        name="OrderController", service="svc", attributes={"file": "OrderController.java"},
    )
    method = GraphNode(
        id="method:svc:OrderController.getOrder", type=NodeType.METHOD,
        name="getOrder", service="svc", attributes={"class": "OrderController"},
    )
    param = GraphNode(
        id="parameter:svc:OrderController.getOrder.id", type=NodeType.PARAMETER,
        name="id", service="svc", attributes={"class": "OrderController", "method": "getOrder"},
    )
    block = GraphNode(
        id="logical_block:svc:OrderController.getOrder.if:10", type=NodeType.LOGICAL_BLOCK,
        name="if", service="svc",
        attributes={"class": "OrderController", "method": "getOrder", "start_line": 10},
    )
    written = _apply_payload(
        graph,
        [owner, method, param, block],
        {
            "class_summary": "HTTP API for orders",
            "spring_role": "Controller",
            "methods": [{
                "name": "getOrder",
                "purpose": "Load one order",
                "parameters": [{"name": "id", "purpose": "Order primary key"}],
                "logic_blocks": [{"kind": "if", "start_line": 10, "purpose": "Reject missing ids"}],
            }],
        },
        fallback_summary="",
        model="gemma4:26b",
    )
    assert written >= 4
    assert graph.nodes[owner.id].attributes["llm_summary"] == "HTTP API for orders"
    assert graph.nodes[method.id].attributes["llm_purpose"] == "Load one order"
    assert graph.nodes[param.id].attributes["llm_purpose"] == "Order primary key"
    assert graph.nodes[block.id].attributes["llm_purpose"] == "Reject missing ids"


def test_ollama_generate_success_returns_message_content(monkeypatch):
    def fake_post(url, json, timeout):
        assert url == Settings().chat_completions_url
        return _FakeResponse(200, json_body={"choices": [{"message": {"content": "hello"}}]})

    monkeypatch.setattr(llm_enrichment.requests, "post", fake_post)
    result = ollama_generate("say hi", Settings())
    assert result == "hello"


def test_ollama_generate_surfaces_server_error_body_after_retries(monkeypatch):
    """A 500 from llama.cpp/Ollama should raise with the response body, not a generic message."""
    monkeypatch.setattr(llm_enrichment.time, "sleep", lambda _seconds: None)

    def fake_post(url, json, timeout):
        return _FakeResponse(500, text='{"error":{"message":"context size exceeded"}}')

    monkeypatch.setattr(llm_enrichment.requests, "post", fake_post)
    with pytest.raises(SystemExit) as exc_info:
        ollama_generate("say hi", Settings())
    assert "context size exceeded" in str(exc_info.value)
    assert "500 server error" in str(exc_info.value)


def test_ollama_generate_404_raises_immediately_without_retry(monkeypatch):
    calls = []

    def fake_post(url, json, timeout):
        calls.append(1)
        return _FakeResponse(404)

    monkeypatch.setattr(llm_enrichment.requests, "post", fake_post)
    with pytest.raises(SystemExit) as exc_info:
        ollama_generate("say hi", Settings())
    assert "404" in str(exc_info.value)
    assert len(calls) == 1
