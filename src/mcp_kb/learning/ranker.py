"""Phase 2 -- offline LightGBM training on accumulated interaction events.

Aggregates ``events.jsonl`` into a per-chunk/per-path training table and fits
a small classifier that predicts whether a chunk/path is "useful" from its
historical feedback signals. The model's output *replaces* the linear
heuristic boost (``OnlineLearner._apply_signal``) once enough data exists and
the model beats the heuristic on a held-out split; until then the caller
should keep using the heuristic (see ``scripts/train_ranker.py`` guardrail).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..logging import get_logger

log = get_logger(__name__)

FEATURE_COLUMNS = ["n_pos", "n_neg", "n_events", "avg_confidence", "recency_days"]


@dataclass
class TrainingResult:
    """Outcome of one scripts/train_ranker.py training run."""

    n_rows: int
    auc: float | None
    model_path: str | None
    trained: bool
    reason: str = ""


def load_events(events_path: Path) -> list[dict[str, Any]]:
    """Read all JSONL interaction events from disk, skipping malformed lines."""
    if not events_path.exists():
        return []
    rows = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            log.debug("ranker_event_line_skipped", error=str(exc))
            continue
    return rows


def build_feature_table(events: list[dict[str, Any]]):
    """Aggregate raw events into one row per (chunk_id or path) with features + label."""
    import pandas as pd

    now = time.time()
    agg: dict[str, dict[str, Any]] = {}
    for ev in events:
        label = ev.get("implicit") or ("positive" if (ev.get("rating") or 0) >= 4 else
                                        "negative" if (ev.get("rating") or 0) and ev.get("rating") <= 2 else None)
        if label is None:
            continue
        ts = ev.get("ts")
        try:
            from datetime import datetime
            age_days = (now - datetime.fromisoformat(ts).timestamp()) / 86400.0
        except Exception:
            age_days = 0.0
        for key in (ev.get("chunk_ids") or []) + (ev.get("paths") or []):
            row = agg.setdefault(key, {"n_pos": 0, "n_neg": 0, "n_events": 0,
                                        "conf_sum": 0.0, "recency_days": age_days})
            row["n_events"] += 1
            row["conf_sum"] += float(ev.get("confidence") or 0.0)
            row["recency_days"] = min(row["recency_days"], age_days)
            if label == "positive":
                row["n_pos"] += 1
            else:
                row["n_neg"] += 1

    records = []
    for key, row in agg.items():
        total = row["n_pos"] + row["n_neg"]
        if total == 0:
            continue
        records.append({
            "id": key,
            "n_pos": row["n_pos"],
            "n_neg": row["n_neg"],
            "n_events": row["n_events"],
            "avg_confidence": row["conf_sum"] / row["n_events"],
            "recency_days": row["recency_days"],
            "label": 1 if row["n_pos"] >= row["n_neg"] else 0,
        })
    return pd.DataFrame.from_records(records)


def train_model(df, *, min_rows: int, min_auc: float, model_path: Path) -> TrainingResult:
    """Train a LightGBM ranking model from query-analytics features; skip if data/AUC too weak."""
    if len(df) < min_rows:
        return TrainingResult(n_rows=len(df), auc=None, model_path=None, trained=False,
                              reason=f"only {len(df)} rows, need >= {min_rows}")
    if df["label"].nunique() < 2:
        return TrainingResult(n_rows=len(df), auc=None, model_path=None, trained=False,
                              reason="single-class labels, cannot train a classifier yet")

    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import train_test_split

    X = df[FEATURE_COLUMNS]
    y = df["label"]
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.25, random_state=42, stratify=y if y.nunique() > 1 else None,
    )
    model = lgb.LGBMClassifier(
        n_estimators=100, max_depth=4, learning_rate=0.05, min_child_samples=3, verbosity=-1,
    )
    model.fit(X_train, y_train)
    try:
        auc = float(roc_auc_score(y_test, model.predict_proba(X_test)[:, 1]))
    except Exception:
        auc = None

    if auc is not None and auc < min_auc:
        return TrainingResult(n_rows=len(df), auc=auc, model_path=None, trained=False,
                              reason=f"AUC {auc:.3f} below guardrail {min_auc}")

    model_path.parent.mkdir(parents=True, exist_ok=True)
    model.booster_.save_model(str(model_path))
    return TrainingResult(n_rows=len(df), auc=auc, model_path=str(model_path), trained=True)


def predict_boosts(model_path: Path, df, *, cap: float = 0.5) -> dict[str, float]:
    """Score every known id and squash proba into a [-cap, cap] boost."""
    import lightgbm as lgb

    if not model_path.exists() or df.empty:
        return {}
    booster = lgb.Booster(model_file=str(model_path))
    proba = booster.predict(df[FEATURE_COLUMNS])
    boosts = {}
    for row_id, p in zip(df["id"], proba):
        boosts[row_id] = round(max(-cap, min(cap, (float(p) - 0.5) * 2 * cap)), 4)
    return boosts
