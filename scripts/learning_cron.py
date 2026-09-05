"""Phase 2/3 scheduler -- runs the LightGBM training pass on a fixed cadence.

Entirely .env-driven:
  LEARNING_TRAIN_ENABLED=true
  LEARNING_TRAIN_INTERVAL_MINUTES=1

This is a plain-Python loop (no OS cron dependency) so it works identically
on Windows/macOS/Linux. For production, wrap it in a systemd unit / Windows
scheduled task / container sidecar that just runs this script once and lets
it loop, or set the interval and invoke it from an actual OS-level cron.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_kb.config import get_settings  # noqa: E402
from mcp_kb.logging import get_logger  # noqa: E402

log = get_logger(__name__)


def main() -> int:
    settings = get_settings()
    if not settings.learning_train_enabled:
        print("[learning_cron] LEARNING_TRAIN_ENABLED=false, exiting.")
        return 0

    interval_s = max(1, settings.learning_train_interval_minutes) * 60
    train_script = Path(__file__).resolve().parent / "train_ranker.py"
    print(f"[learning_cron] running {train_script} every "
          f"{settings.learning_train_interval_minutes} minute(s). Ctrl+C to stop.")

    while True:
        try:
            subprocess.run([sys.executable, str(train_script)], check=False)
        except Exception as exc:
            log.warning("learning_cron_run_failed", error=str(exc))
        time.sleep(interval_s)


if __name__ == "__main__":
    raise SystemExit(main())
