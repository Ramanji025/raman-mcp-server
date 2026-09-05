"""Unit tests for RBAC (security/rbac.py) and audit logging (security/audit_log.py)."""
from __future__ import annotations

import json

from mcp_kb.security.audit_log import AuditLog
from mcp_kb.security.rbac import RBACPolicy


def _settings_disabled(settings):
    settings.rbac_enabled = False
    return settings


def test_rbac_disabled_by_default_allows_everything(settings):
    policy = RBACPolicy(settings)
    decision = policy.check("analyse_exception", repo="rx-order")
    assert decision.allowed is True


def test_rbac_enabled_with_missing_policy_file_allows_all(settings, tmp_path):
    settings.rbac_enabled = True
    settings.rbac_policy_path = tmp_path / "nonexistent_rbac.yaml"
    policy = RBACPolicy(settings)
    decision = policy.check("analyse_exception")
    assert decision.allowed is True


def test_rbac_denies_tool_in_deny_list(settings, tmp_path, monkeypatch):
    policy_file = tmp_path / "rbac.yaml"
    policy_file.write_text(
        "default_role: viewer\n"
        "roles:\n"
        "  viewer:\n"
        "    tools: ['*']\n"
        "    deny_tools: ['analyse_exception']\n"
        "    repos: ['*']\n"
        "principals:\n"
        "  local: viewer\n",
        encoding="utf-8",
    )
    settings.rbac_enabled = True
    settings.rbac_policy_path = policy_file
    monkeypatch.setenv("MCP_KB_PRINCIPAL", "local")
    policy = RBACPolicy(settings)
    denied = policy.check("analyse_exception")
    allowed = policy.check("search_code")
    assert denied.allowed is False
    assert allowed.allowed is True


def test_rbac_restricts_repo_scope(settings, tmp_path, monkeypatch):
    policy_file = tmp_path / "rbac.yaml"
    policy_file.write_text(
        "default_role: restricted\n"
        "roles:\n"
        "  restricted:\n"
        "    tools: ['*']\n"
        "    repos: ['rx-order']\n"
        "principals:\n"
        "  local: restricted\n",
        encoding="utf-8",
    )
    settings.rbac_enabled = True
    settings.rbac_policy_path = policy_file
    monkeypatch.setenv("MCP_KB_PRINCIPAL", "local")
    policy = RBACPolicy(settings)
    allowed = policy.check("explain_service", repo="rx-order")
    denied = policy.check("explain_service", repo="rx-pricing")
    assert allowed.allowed is True
    assert denied.allowed is False


def test_audit_log_json_backend_records_and_reads_back(settings):
    settings.postgres_enabled = False
    log = AuditLog(settings)
    log.record(principal="local", tool="explain_service", query={"service_name": "rx-order"},
               repo_scope="rx-order", allowed=True, denial_reason=None)
    entries = log.recent(limit=10)
    assert len(entries) == 1
    assert entries[0]["tool"] == "explain_service"
    assert entries[0]["allowed"] is True


def test_audit_log_disabled_records_nothing(settings):
    settings.audit_log_enabled = False
    log = AuditLog(settings)
    log.record(principal="local", tool="x", query={}, repo_scope=None, allowed=True,
              denial_reason=None)
    assert log.recent(limit=10) == []
