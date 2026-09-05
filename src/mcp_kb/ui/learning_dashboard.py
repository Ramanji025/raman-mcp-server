"""Plotly Dash dashboard for the online-learning subsystem.

Run::

    python -m mcp_kb.ui.learning_dashboard

Port / refresh interval are .env driven (LEARNING_DASHBOARD_PORT,
LEARNING_DASHBOARD_REFRESH_S). Reads the same files the learner writes:
data/learning/state.json, data/learning/events.jsonl,
data/models/ranker_metrics.json.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import plotly.express as px
from dash import Dash, Input, Output, dcc, html

from ..config import get_settings

settings = get_settings()
LEARNING_DIR = Path(settings.repos_root).parent / "learning"
MODEL_DIR = Path(settings.learning_model_path).parent
STATE_PATH = LEARNING_DIR / "state.json"
EVENTS_PATH = LEARNING_DIR / "events.jsonl"
METRICS_PATH = MODEL_DIR / "ranker_metrics.json"

app = Dash(__name__, title="MCP-KB Learning Dashboard")
app.layout = html.Div([
    html.H2("MCP Microservices KB \u2014 Online Learning"),
    dcc.Interval(id="refresh", interval=settings.learning_dashboard_refresh_s * 1000, n_intervals=0),
    html.Div(id="kpi-row", style={"display": "flex", "gap": "24px", "marginBottom": "16px"}),
    html.Div([
        dcc.Graph(id="interactions-over-time"),
        dcc.Graph(id="top-boosts"),
    ], style={"display": "flex", "gap": "16px"}),
    html.Div([
        dcc.Graph(id="rating-distribution"),
        html.Div(id="model-card"),
    ], style={"display": "flex", "gap": "16px"}),
    html.H4("Learned aliases"),
    html.Div(id="alias-table"),
])


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_events() -> pd.DataFrame:
    if not EVENTS_PATH.exists():
        return pd.DataFrame()
    rows = []
    for line in EVENTS_PATH.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return pd.DataFrame(rows)


def _load_metrics() -> dict:
    if not METRICS_PATH.exists():
        return {}
    try:
        return json.loads(METRICS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


@app.callback(
    Output("kpi-row", "children"),
    Output("interactions-over-time", "figure"),
    Output("top-boosts", "figure"),
    Output("rating-distribution", "figure"),
    Output("model-card", "children"),
    Output("alias-table", "children"),
    Input("refresh", "n_intervals"),
)
def refresh(_n):
    """Dash callback: rebuild all dashboard panels from the latest learning state/events/metrics."""
    state = _load_state()
    events = _load_events()
    metrics = _load_metrics()

    def kpi(label, value):
        return html.Div([html.Div(label, style={"fontSize": "12px", "color": "#888"}),
                          html.Div(str(value), style={"fontSize": "24px", "fontWeight": "bold"})],
                         style={"border": "1px solid #ddd", "padding": "12px", "borderRadius": "8px"})

    kpis = [
        kpi("Interactions", state.get("n_interactions", 0)),
        kpi("Ratings", state.get("n_ratings", 0)),
        kpi("Implicit +", state.get("n_implicit_pos", 0)),
        kpi("Implicit -", state.get("n_implicit_neg", 0)),
        kpi("Aliases", len(state.get("aliases", {}))),
        kpi("Boosted chunks", len(state.get("chunk_boosts", {}))),
    ]

    if not events.empty and "ts" in events:
        events["ts_dt"] = pd.to_datetime(events["ts"], errors="coerce")
        ts_fig = px.histogram(events, x="ts_dt", title="Interactions over time", nbins=30)
    else:
        ts_fig = px.scatter(title="No interactions yet")

    boosts = {**state.get("path_boosts", {}), **state.get("chunk_boosts", {})}
    if boosts:
        bdf = pd.DataFrame(sorted(boosts.items(), key=lambda kv: -kv[1])[:15], columns=["id", "boost"])
        boost_fig = px.bar(bdf, x="boost", y="id", orientation="h", title="Top learned boosts")
    else:
        boost_fig = px.scatter(title="No boosts yet")

    if not events.empty and "rating" in events and events["rating"].notna().any():
        rating_fig = px.histogram(events.dropna(subset=["rating"]), x="rating", title="Rating distribution")
    else:
        rating_fig = px.scatter(title="No ratings yet")

    if metrics:
        model_card = html.Div([
            html.H4("Latest LightGBM training run"),
            html.P(f"Trained: {metrics.get('trained')}  |  Rows: {metrics.get('n_rows')}  |  "
                   f"AUC: {metrics.get('auc')}"),
            html.P(f"Reason: {metrics.get('reason', '')}"),
        ], style={"border": "1px solid #ddd", "padding": "12px", "borderRadius": "8px"})
    else:
        model_card = html.P("No training run yet.")

    aliases = state.get("aliases", {})
    alias_rows = [html.Tr([html.Td(k), html.Td(v)]) for k, v in list(aliases.items())[:25]]
    alias_table = html.Table(
        [html.Tr([html.Th("term"), html.Th("entity")])] + alias_rows,
        style={"width": "100%", "borderCollapse": "collapse"},
    )

    return kpis, ts_fig, boost_fig, rating_fig, model_card, alias_table


def main() -> None:
    """Entrypoint: run the Dash learning dashboard server."""
    app.run(host="0.0.0.0", port=settings.learning_dashboard_port, debug=False)


if __name__ == "__main__":
    main()
