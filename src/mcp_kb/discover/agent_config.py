"""Follow-on gap 5: agent auto-discovery and MCP config writing.

Mirrors codebase-memory-mcp's client auto-configuration: detect which AI
coding agents are installed on this machine (by checking for their known
config file/directory), then write (merge, never clobber) an MCP server
entry pointing at this project's installed `mcp-kb-server` executable.

Supported clients (bounded, real implementations — not a stub list):
  - VS Code / VS Code Insiders (global `mcp.json` under the user config dir)
  - Claude Desktop (`claude_desktop_config.json`)
  - Cursor (`~/.cursor/mcp.json`)

Writes are transactional per-file (read-merge-write) and additive: an
existing `mcpServers`/`servers` entry for any *other* server name is left
untouched; only the `microservices-kb` entry is created/updated.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..logging import get_logger

log = get_logger(__name__)

_SERVER_NAME = "microservices-kb"


@dataclass
class ClientTarget:
    """One agent client's config file location + JSON shape."""

    name: str
    config_path: Path
    servers_key: str  # "mcpServers" (Claude/Cursor) or "servers" (VS Code mcp.json)


def _user_config_dir() -> Path:
    """Return the platform-appropriate per-user application config root."""
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def discover_clients() -> list[ClientTarget]:
    """Return every supported client whose config directory already exists
    on this machine (i.e. the client itself is installed) — never creates a
    client's own directory, only the config file inside an existing one."""
    base = _user_config_dir()
    candidates = [
        ClientTarget("Claude Desktop", base / "Claude" / "claude_desktop_config.json", "mcpServers"),
        ClientTarget("VS Code", base / "Code" / "User" / "mcp.json", "servers"),
        ClientTarget("VS Code Insiders", base / "Code - Insiders" / "User" / "mcp.json", "servers"),
        ClientTarget("Cursor", Path.home() / ".cursor" / "mcp.json", "mcpServers"),
    ]
    return [c for c in candidates if c.config_path.parent.exists()]


def _server_executable() -> str:
    """Path to the `mcp-kb-server` entry point installed in the *current*
    Python environment (works for venv, system Python, or an editable
    install alike) — never guesses a hardcoded path."""
    venv_bin = Path(sys.executable).parent
    exe_name = "mcp-kb-server.exe" if sys.platform == "win32" else "mcp-kb-server"
    candidate = venv_bin / exe_name
    return str(candidate) if candidate.exists() else exe_name  # fall back to PATH lookup


def _server_entry(repos_root: Path) -> dict[str, Any]:
    return {
        "command": _server_executable(),
        "env": {
            "MCP_KB_REPOS_ROOT": str(repos_root),
        },
    }


def write_client_config(target: ClientTarget, repos_root: Path, *, dry_run: bool = False) -> bool:
    """Merge the `microservices-kb` server entry into one client's config.

    Returns True if the file was written (or would be, in dry-run mode).
    Reads and preserves any existing content/servers untouched.
    """
    existing: dict[str, Any] = {}
    if target.config_path.exists():
        try:
            existing = json.loads(target.config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("agent_config_unparseable_skipping", client=target.name,
                       path=str(target.config_path))
            return False

    servers = existing.setdefault(target.servers_key, {})
    servers[_SERVER_NAME] = _server_entry(repos_root)

    if dry_run:
        log.info("agent_config_dry_run", client=target.name, path=str(target.config_path))
        return True

    target.config_path.parent.mkdir(parents=True, exist_ok=True)
    target.config_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    log.info("agent_config_written", client=target.name, path=str(target.config_path))
    return True


def install_all(repos_root: Path, *, dry_run: bool = False) -> list[tuple[str, Path, bool]]:
    """Detect + configure every installed client. Returns [(client_name, path, wrote)]."""
    results = []
    for target in discover_clients():
        wrote = write_client_config(target, repos_root, dry_run=dry_run)
        results.append((target.name, target.config_path, wrote))
    return results


def main() -> None:
    """CLI entrypoint (`mcp-kb-install-agents`)."""
    import argparse

    from ..config import get_settings

    parser = argparse.ArgumentParser(description="Auto-configure detected AI coding agent clients.")
    parser.add_argument("--dry-run", action="store_true", help="Detect and log, but don't write files.")
    args = parser.parse_args()

    settings = get_settings()
    results = install_all(Path(settings.repos_root).resolve(), dry_run=args.dry_run)
    if not results:
        print("No supported agent clients detected on this machine.")
        return
    for name, path, wrote in results:
        status = "would write" if args.dry_run else ("configured" if wrote else "skipped (unparseable)")
        print(f"  {name}: {status} -> {path}")


if __name__ == "__main__":
    main()
