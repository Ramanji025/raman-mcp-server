"""Role-based access control: repository-level permissions per principal.

Disabled by default (``RBAC_ENABLED=false``) so behaviour is unchanged for
the current single-developer, local-first deployment model. When enabled,
reads a simple YAML policy (``config/rbac.yaml`` by default) mapping
principals to roles and roles to allowed tools/repositories:

    default_role: viewer
    roles:
      viewer:
        tools: ["*"]              # allowed tool names, or "*" for all
        deny_tools: ["analyse_exception", "classify_incident"]
        repos: ["*"]              # allowed repos (service_name/repo_name), or "*"
      admin:
        tools: ["*"]
        repos: ["*"]
    principals:
      local: admin                # principal -> role name

The "principal" for a local stdio MCP server (single developer machine) is
resolved from ``MCP_KB_PRINCIPAL`` (default ``"local"``); in an HTTP/multi-
tenant deployment this would instead come from request auth middleware —
that wiring is intentionally left to the deployment layer (see
docs/ENTERPRISE_ARCHITECTURE.md, Security section) since FastMCP's HTTP
transport auth is deployment-specific.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..config import Settings
from ..logging import get_logger

log = get_logger(__name__)


@dataclass
class AccessDecision:
    """Result of an RBAC access check: allowed or not, with a reason."""

    allowed: bool
    reason: str = ""


_DEFAULT_ROLE_ALLOW_ALL = {"tools": ["*"], "repos": ["*"], "deny_tools": []}


class RBACPolicy:
    """Loaded RBAC policy + access-check logic."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._roles: dict[str, dict[str, Any]] = {}
        self._principals: dict[str, str] = {}
        self._default_role = "viewer"
        if settings.rbac_enabled:
            self._load(settings.rbac_policy_path)

    def _load(self, path: Path) -> None:
        if not path.exists():
            log.warning("rbac_policy_missing_allowing_all", path=str(path))
            return
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        self._default_role = raw.get("default_role", "viewer")
        self._roles = raw.get("roles", {})
        self._principals = raw.get("principals", {})
        log.info("rbac_policy_loaded", roles=list(self._roles), principals=list(self._principals))

    def resolve_principal(self) -> str:
        """Return the current principal id (from MCP_KB_PRINCIPAL, defaulting to 'local')."""
        return os.environ.get("MCP_KB_PRINCIPAL", "local")

    def _role_for(self, principal: str) -> dict[str, Any]:
        role_name = self._principals.get(principal, self._default_role)
        return self._roles.get(role_name, _DEFAULT_ROLE_ALLOW_ALL)

    def check(self, tool_name: str, *, repo: str | None = None,
              principal: str | None = None) -> AccessDecision:
        """Check whether `principal` may call `tool_name` (and access `repo`, if given)."""
        if not self._settings.rbac_enabled:
            return AccessDecision(allowed=True, reason="rbac_disabled")
        principal = principal or self.resolve_principal()
        role = self._role_for(principal)

        deny_tools = role.get("deny_tools", [])
        if tool_name in deny_tools:
            return AccessDecision(allowed=False,
                                  reason=f"tool '{tool_name}' explicitly denied for principal "
                                         f"'{principal}'")

        allowed_tools = role.get("tools", ["*"])
        if "*" not in allowed_tools and tool_name not in allowed_tools:
            return AccessDecision(allowed=False,
                                  reason=f"tool '{tool_name}' not in allowed tools for "
                                         f"principal '{principal}'")

        if repo is not None:
            allowed_repos = role.get("repos", ["*"])
            if "*" not in allowed_repos and repo not in allowed_repos:
                return AccessDecision(allowed=False,
                                      reason=f"repo '{repo}' not accessible to principal "
                                             f"'{principal}'")

        return AccessDecision(allowed=True, reason="ok")


_POLICY: RBACPolicy | None = None


def get_rbac_policy(settings: Settings) -> RBACPolicy:
    """Return the process-wide RBACPolicy singleton, creating it on first use."""
    global _POLICY
    if _POLICY is None:
        _POLICY = RBACPolicy(settings)
    return _POLICY
