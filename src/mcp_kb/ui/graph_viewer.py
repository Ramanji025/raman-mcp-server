"""Phase 7: local graph visualization UI.

A small, dependency-light Starlette app (uvicorn/starlette are already
transitive deps via fastmcp's HTTP transport — no new dependency) serving:

  GET  /                    interactive graph page (vis-network via CDN)
  GET  /api/services        list of indexed service names (for the dropdown)
  GET  /api/graph?service=  {nodes:[...], edges:[...]} JSON for one service
                            (or the whole graph, capped, if omitted)

Mirrors codebase-memory-mcp's built-in `localhost:9749` graph UI, scoped to
what's practical without vendoring a JS bundle: a 2D force-directed graph
(vis-network, loaded from a CDN) rather than the 3D Three.js viewer CBM
ships inside its compiled binary. Requires GRAPH_BACKEND=neo4j (uses the
same read-only Cypher path as graph/snapshot.py's export).

Run standalone:  python -m mcp_kb.ui.graph_viewer
Or auto-started by the main server when GRAPH_UI_ENABLED=true.
"""
from __future__ import annotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from ..config import Settings, get_settings
from ..logging import get_logger

log = get_logger(__name__)

# Node label -> color, so controllers/entities/tables are visually distinct
# at a glance (mirrors CBM's label-colored 3D nodes).
_NODE_COLORS = {
    "Service": "#4C6EF5", "Controller": "#12B886", "Endpoint": "#15AABF",
    "ServiceLayer": "#40C057", "Repository": "#F59F00", "Entity": "#E8590C",
    "Table": "#868E96", "KafkaProducer": "#BE4BDB", "KafkaConsumer": "#BE4BDB",
    "Topic": "#9C36B5", "Method": "#748FFC", "Class": "#ADB5BD",
    "Interface": "#63E6BE", "File": "#CED4DA",
}
_DEFAULT_COLOR = "#495057"
_MAX_UI_NODES = 500


def _graph_json(settings: Settings, service: str | None) -> dict:
    from ..graph.factory import get_graph_store

    graph = get_graph_store(settings)
    nodes = graph.all_nodes(service)[:_MAX_UI_NODES]
    node_ids = {n["id"] for n in nodes}

    vis_nodes = [
        {"id": n["id"], "label": n.get("name") or n["id"],
         "group": n.get("type"), "color": _NODE_COLORS.get(n.get("type"), _DEFAULT_COLOR),
         "title": f"{n.get('type')}: {n.get('name')} ({n.get('service') or 'shared'})"}
        for n in nodes
    ]

    edges: list[dict] = []
    try:
        if service:
            query = ("MATCH (a:KBNode)-[r]->(b:KBNode) WHERE a.service = $service "
                     "OR b.service = $service RETURN a.id AS src, b.id AS dst, type(r) AS type")
            rows = graph.run_cypher(query, {"service": service}, max_rows=2000)
        else:
            rows = graph.run_cypher(
                "MATCH (a:KBNode)-[r]->(b:KBNode) RETURN a.id AS src, b.id AS dst, type(r) AS type",
                max_rows=2000,
            )
        edges = [{"from": r["src"], "to": r["dst"], "label": r["type"]}
                 for r in rows if r["src"] in node_ids and r["dst"] in node_ids]
    except NotImplementedError:
        pass  # NetworkX/local backend: nodes only, no ad-hoc Cypher available

    return {"nodes": vis_nodes, "edges": edges,
            "truncated": len(nodes) >= _MAX_UI_NODES}


async def index(request: Request) -> HTMLResponse:
    return HTMLResponse(_PAGE_HTML)


async def api_services(request: Request) -> JSONResponse:
    from ..graph.factory import get_graph_store

    settings = get_settings()
    graph = get_graph_store(settings)
    return JSONResponse({"services": graph.services()})


async def api_graph(request: Request) -> JSONResponse:
    settings = get_settings()
    service = request.query_params.get("service") or None
    return JSONResponse(_graph_json(settings, service))


app = Starlette(routes=[
    Route("/", index),
    Route("/api/services", api_services),
    Route("/api/graph", api_graph),
])

_PAGE_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>MCP Knowledge Graph</title>
  <script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
  <style>
    body { margin: 0; font-family: system-ui, sans-serif; }
    #toolbar { padding: 8px 12px; background: #212529; color: #fff; display: flex; gap: 12px; align-items: center; }
    #toolbar select, #toolbar button { padding: 4px 8px; }
    #graph { width: 100vw; height: calc(100vh - 44px); }
    #status { color: #adb5bd; font-size: 12px; }
  </style>
</head>
<body>
  <div id="toolbar">
    <strong>MCP Knowledge Graph</strong>
    <select id="service"><option value="">(all services, capped)</option></select>
    <button onclick="loadGraph()">Refresh</button>
    <span id="status"></span>
  </div>
  <div id="graph"></div>
  <script>
    let network = null;
    async function loadServices() {
      const res = await fetch("/api/services");
      const data = await res.json();
      const sel = document.getElementById("service");
      for (const s of data.services) {
        const opt = document.createElement("option");
        opt.value = s; opt.textContent = s;
        sel.appendChild(opt);
      }
    }
    async function loadGraph() {
      const service = document.getElementById("service").value;
      const url = service ? `/api/graph?service=${encodeURIComponent(service)}` : "/api/graph";
      const res = await fetch(url);
      const data = await res.json();
      document.getElementById("status").textContent =
        `${data.nodes.length} nodes, ${data.edges.length} edges` +
        (data.truncated ? " (truncated — pick a service for full detail)" : "");
      const nodes = new vis.DataSet(data.nodes);
      const edges = new vis.DataSet(data.edges);
      const container = document.getElementById("graph");
      const options = {
        nodes: { shape: "dot", size: 10, font: { color: "#212529" } },
        edges: { arrows: "to", color: { color: "#ced4da" }, font: { size: 8, align: "middle" } },
        physics: { stabilization: true, barnesHut: { gravitationalConstant: -8000 } },
      };
      network = new vis.Network(container, { nodes, edges }, options);
    }
    loadServices().then(loadGraph);
  </script>
</body>
</html>
"""


def main() -> None:
    """CLI entrypoint (`mcp-kb-ui`): run the graph viewer standalone."""
    import uvicorn

    settings = get_settings()
    log.info("graph_ui_start", port=settings.graph_ui_port)
    uvicorn.run(app, host="127.0.0.1", port=settings.graph_ui_port, log_level="warning")


if __name__ == "__main__":
    main()
