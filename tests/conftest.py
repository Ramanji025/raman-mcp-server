"""Shared pytest fixtures."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

FIXTURE_REPO = Path(__file__).parent / "fixtures" / "rx-order-service"


@pytest.fixture(scope="session")
def fixture_repo() -> Path:
    return FIXTURE_REPO


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    """Isolated settings pointing at a temp data dir and JSON metadata backend."""
    monkeypatch.setenv("MCP_KB_REPOS_ROOT", str(tmp_path / "repos"))
    monkeypatch.setenv("POSTGRES_ENABLED", "false")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fastembed")
    # config.yaml lives at repo root of the project.
    from mcp_kb.config import Settings, get_settings

    get_settings.cache_clear()
    project_root = Path(__file__).resolve().parents[1]
    s = Settings(config_yaml=project_root / "config" / "config.yaml")
    return s


def qdrant_available(url: str = "http://localhost:6333") -> bool:
    import urllib.request

    try:
        urllib.request.urlopen(url, timeout=1.0)
        return True
    except Exception:
        return False
