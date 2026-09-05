"""CLI entry point: one offline LightGBM training pass over learning events.

Run manually::

    python scripts/train_ranker.py

The MCP server (`mcp-kb-server`) can also run this on a schedule in-process --
see LEARNING_TRAIN_ENABLED / LEARNING_TRAIN_INTERVAL_MINUTES in .env, wired
via mcp_kb.learning.scheduler.start_background_jobs(). This script and
`scripts/learning_cron.py` remain for standalone/manual use.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_kb.config import get_settings  # noqa: E402
from mcp_kb.learning.scheduler import run_training_pass  # noqa: E402


def main() -> int:
    metrics = run_training_pass(get_settings())
    if not metrics["trained"]:
        print(f"[train_ranker] skipped: {metrics['reason']}")
        return 0
    print(f"[train_ranker] trained on {metrics['n_rows']} rows, AUC={metrics['auc']:.3f}, "
          f"model={metrics['model_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
