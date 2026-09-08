"""Follow-on gap 4: tests for the single-instance PID-file lock."""
from __future__ import annotations

import os
from pathlib import Path

from mcp_kb.ingestion.single_instance import SingleInstanceLock, single_instance


def test_first_process_acquires_lock(tmp_path: Path):
    lock = SingleInstanceLock(tmp_path, "watcher")
    assert lock.acquire() is True
    assert (tmp_path / "watcher.lock").read_text(encoding="utf-8").strip() == str(os.getpid())


def test_second_instance_cannot_acquire_while_first_holds_it(tmp_path: Path, monkeypatch):
    """Simulate a genuinely different, still-alive process holding the lock."""
    lock_path = tmp_path / "watcher.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    other_pid = os.getpid() + 1
    lock_path.write_text(str(other_pid), encoding="utf-8")
    monkeypatch.setattr("mcp_kb.ingestion.single_instance._pid_is_alive",
                        lambda pid: pid == other_pid)

    second = SingleInstanceLock(tmp_path, "watcher")
    assert second.acquire() is False


def test_stale_lock_from_dead_pid_is_reclaimed(tmp_path: Path):
    lock_path = tmp_path / "watcher.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # A PID astronomically unlikely to be alive.
    lock_path.write_text("999999", encoding="utf-8")

    lock = SingleInstanceLock(tmp_path, "watcher")
    assert lock.acquire() is True
    assert lock_path.read_text(encoding="utf-8").strip() == str(os.getpid())


def test_release_removes_lock_file_owned_by_this_process(tmp_path: Path):
    lock = SingleInstanceLock(tmp_path, "watcher")
    lock.acquire()
    lock.release()
    assert not (tmp_path / "watcher.lock").exists()


def test_context_manager_yields_true_and_cleans_up(tmp_path: Path):
    with single_instance(tmp_path, "ui") as acquired:
        assert acquired is True
        assert (tmp_path / "ui.lock").exists()
    assert not (tmp_path / "ui.lock").exists()
