"""In-process background jobs for the online-learning subsystem.

Started from ``mcp-kb-server`` (see ``server.py``) so ``LEARNING_TRAIN_ENABLED``
and ``LEARNING_DASHBOARD_AUTOSTART`` alone control the cron trainer / dashboard
-- no separate ``scripts/train_ranker.py`` / ``scripts/learning_cron.py`` /
``python -m mcp_kb.ui.learning_dashboard`` process to manage by hand.

Both jobs run as daemon threads: they never block server startup and are
killed automatically when the server process exits.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from ..config import Settings
from ..logging import get_logger

log = get_logger(__name__)


def run_training_pass(settings: Settings) -> dict:
    """One offline LightGBM training pass. Shared by the CLI script and the cron thread."""
    from .ranker import build_feature_table, load_events, predict_boosts, train_model

    events_path = Path(settings.repos_root).parent / "learning" / "events.jsonl"
    model_path = Path(settings.learning_model_path)
    metrics_path = model_path.with_name("ranker_metrics.json")

    events = load_events(events_path)
    df = build_feature_table(events)
    result = train_model(
        df,
        min_rows=settings.learning_min_training_rows,
        min_auc=settings.learning_model_min_auc,
        model_path=model_path,
    )

    metrics = {
        "trained_at": time.time(), "n_rows": result.n_rows, "auc": result.auc,
        "trained": result.trained, "reason": result.reason, "model_path": result.model_path,
    }
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")

    if result.trained:
        boosts = predict_boosts(model_path, df, cap=settings.learning_model_boost_cap)
        model_path.with_name("model_boosts.json").write_text(
            json.dumps(boosts, indent=2), encoding="utf-8",
        )
    return metrics


def _training_loop(settings: Settings) -> None:
    interval_s = max(1, settings.learning_train_interval_minutes) * 60
    log.info("learning_cron_started", interval_minutes=settings.learning_train_interval_minutes)
    while True:
        try:
            metrics = run_training_pass(settings)
            log.info("learning_cron_run", trained=metrics["trained"], n_rows=metrics["n_rows"],
                     auc=metrics["auc"])
        except Exception as exc:
            log.warning("learning_cron_run_failed", error=str(exc))
        time.sleep(interval_s)


def _dashboard_loop(settings: Settings) -> None:
    from ..ui.learning_dashboard import app

    log.info("learning_dashboard_started", port=settings.learning_dashboard_port)
    try:
        app.run(host="0.0.0.0", port=settings.learning_dashboard_port, debug=False, use_reloader=False)
    except Exception as exc:
        log.warning("learning_dashboard_failed", error=str(exc))


def start_background_jobs(settings: Settings) -> None:
    """Spawn the cron trainer / dashboard as daemon threads, per .env flags."""
    if not settings.learning_enabled:
        return
    if settings.learning_train_enabled:
        threading.Thread(target=_training_loop, args=(settings,), daemon=True,
                         name="learning-cron").start()
    if settings.learning_dashboard_autostart:
        threading.Thread(target=_dashboard_loop, args=(settings,), daemon=True,
                         name="learning-dashboard").start()
