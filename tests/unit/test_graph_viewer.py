"""Phase 7: tests for the local graph visualization UI (Starlette app)."""
from __future__ import annotations

from starlette.testclient import TestClient

from mcp_kb.ui.graph_viewer import app


def test_index_serves_html_page():
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "vis-network" in resp.text


def test_api_services_returns_json_list(monkeypatch, settings):
    monkeypatch.setenv("GRAPH_BACKEND", "networkx")
    client = TestClient(app)
    resp = client.get("/api/services")
    assert resp.status_code == 200
    assert "services" in resp.json()


def test_api_graph_returns_nodes_and_edges_shape(monkeypatch, settings):
    monkeypatch.setenv("GRAPH_BACKEND", "networkx")
    client = TestClient(app)
    resp = client.get("/api/graph")
    assert resp.status_code == 200
    data = resp.json()
    assert "nodes" in data and "edges" in data and "truncated" in data
