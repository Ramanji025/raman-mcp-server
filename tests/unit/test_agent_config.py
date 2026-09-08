"""Follow-on gap 5: tests for agent client auto-discovery/config writing."""
from __future__ import annotations

import json
from pathlib import Path

from mcp_kb.discover.agent_config import ClientTarget, write_client_config


def test_write_client_config_creates_new_file(tmp_path: Path):
    target = ClientTarget("Claude Desktop", tmp_path / "claude_desktop_config.json", "mcpServers")
    assert write_client_config(target, tmp_path / "repos") is True

    data = json.loads(target.config_path.read_text(encoding="utf-8"))
    assert "microservices-kb" in data["mcpServers"]
    assert data["mcpServers"]["microservices-kb"]["env"]["MCP_KB_REPOS_ROOT"] == str(tmp_path / "repos")


def test_write_client_config_preserves_other_existing_servers(tmp_path: Path):
    config_path = tmp_path / "mcp.json"
    config_path.write_text(json.dumps({
        "servers": {"some-other-server": {"command": "other-tool"}},
    }), encoding="utf-8")
    target = ClientTarget("VS Code", config_path, "servers")

    write_client_config(target, tmp_path / "repos")

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["servers"]["some-other-server"] == {"command": "other-tool"}
    assert "microservices-kb" in data["servers"]


def test_write_client_config_is_idempotent(tmp_path: Path):
    target = ClientTarget("Cursor", tmp_path / "mcp.json", "mcpServers")
    write_client_config(target, tmp_path / "repos")
    write_client_config(target, tmp_path / "repos")  # second run must not duplicate/corrupt

    data = json.loads(target.config_path.read_text(encoding="utf-8"))
    assert list(data["mcpServers"].keys()) == ["microservices-kb"]


def test_write_client_config_skips_unparseable_json(tmp_path: Path):
    config_path = tmp_path / "broken.json"
    config_path.write_text("{ not valid json", encoding="utf-8")
    target = ClientTarget("Claude Desktop", config_path, "mcpServers")

    assert write_client_config(target, tmp_path / "repos") is False
    assert config_path.read_text(encoding="utf-8") == "{ not valid json"  # untouched, not clobbered


def test_dry_run_never_writes_file(tmp_path: Path):
    target = ClientTarget("Claude Desktop", tmp_path / "claude_desktop_config.json", "mcpServers")
    assert write_client_config(target, tmp_path / "repos", dry_run=True) is True
    assert not target.config_path.exists()
