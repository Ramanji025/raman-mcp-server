"""Follow-on gap 4: single-instance admission barrier for background services.

Mirrors (in scope, not full mechanism) codebase-memory-mcp's daemon
admission barrier: multiple MCP server processes on one machine (one per
connected agent client) must not each spin up their own watcher/UI thread
against the same repo root — that wastes resources and can race on git
operations. This is a lightweight, portable (Windows/macOS/Linux) advisory
lock using a PID file, not a full IPC daemon — sufficient for "only one
watcher/UI per machine", which is the actual problem multi-session
duplication causes here (unlike CBM, our watcher/UI don't need to share
in-memory index state across processes since the graph itself is external
to the process, in Neo4j/NetworkX-JSON).
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from ..logging import get_logger

log = get_logger(__name__)


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
            return True
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError, PermissionError):
        return os.name != "nt"  # PermissionError on POSIX still means "alive, not ours"
    except Exception:  # pragma: no cover - defensive: never let this crash startup
        return False


class SingleInstanceLock:
    """Advisory, PID-file-based lock: `acquire()` returns True if this process
    is (or becomes) the sole holder for `name`; stale locks from a dead PID
    are automatically reclaimed."""

    def __init__(self, lock_dir: Path, name: str) -> None:
        self._path = lock_dir / f"{name}.lock"
        self._acquired = False

    def acquire(self) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            try:
                existing_pid = int(self._path.read_text(encoding="utf-8").strip() or "0")
            except (OSError, ValueError):
                existing_pid = 0
            if existing_pid and existing_pid != os.getpid() and _pid_is_alive(existing_pid):
                log.info("single_instance_lock_held", name=self._path.name, holder_pid=existing_pid)
                return False
            log.info("single_instance_lock_reclaimed_stale", name=self._path.name,
                     stale_pid=existing_pid)
        self._path.write_text(str(os.getpid()), encoding="utf-8")
        self._acquired = True
        return True

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            if self._path.exists() and self._path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                self._path.unlink()
        except OSError:
            pass
        self._acquired = False


@contextmanager
def single_instance(lock_dir: Path, name: str):
    """Context manager: yields True if this process acquired the lock for
    `name` (caller should run its background service), False otherwise
    (another live process already owns it — caller should skip, not fail)."""
    lock = SingleInstanceLock(lock_dir, name)
    acquired = lock.acquire()
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()
