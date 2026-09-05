"""Unit tests for the sensitive-code/PII scanner (security/sensitive_scanner.py)."""
from __future__ import annotations

from mcp_kb.config import Settings
from mcp_kb.security.sensitive_scanner import (
    is_sensitive,
    redact,
    scan_and_redact_chunk_text,
    scan_text,
)

_JAVA_WITH_JDBC_PASSWORD = (
    "public class Config {\n"
    "    public void connect() {\n"
    "        String url = \"jdbc:postgresql://localhost:5432/db?password=SuperSecret123\";\n"
    "    }\n"
    "}\n"
)

_NORMAL_JAVA = (
    "public class OrderService {\n"
    "    long orderId = 1234567890123456L;\n"
    "    int line = 42;\n"
    "    private String description; // no secrets here\n"
    "}\n"
)


def test_scan_detects_jdbc_password():
    findings = scan_text(_JAVA_WITH_JDBC_PASSWORD)
    kinds = {f.kind for f in findings}
    assert "jdbc_credentials" in kinds


def test_scan_detects_generic_api_key():
    findings = scan_text('String cfg = "api_key=sk_live_abcdef1234567890abcdef";')
    kinds = {f.kind for f in findings}
    assert "generic_api_key" in kinds


def test_scan_detects_hardcoded_password():
    findings = scan_text('password = "mySecretPass123"')
    kinds = {f.kind for f in findings}
    assert "hardcoded_password" in kinds


def test_scan_detects_aws_access_key():
    findings = scan_text("aws.key=AKIAABCDEFGHIJKLMNOP")
    kinds = {f.kind for f in findings}
    assert "aws_access_key_id" in kinds


def test_normal_numeric_code_is_not_flagged_as_sensitive():
    # Long numeric literals / line numbers must not trip the secret scanner
    # (credit_card requires explicit 4-4-4-4 grouping, not bare digit runs).
    assert is_sensitive(_NORMAL_JAVA) is False


def test_email_is_pii_only_not_flagged_as_secret_by_default():
    text = "// @author jane.doe@example.com"
    assert is_sensitive(text, include_pii=False) is False
    assert is_sensitive(text, include_pii=True) is True


def test_credit_card_requires_explicit_grouping():
    grouped = "card: 4111-1111-1111-1111"
    ungrouped_long_number = "hash: 4111111111111111111"
    assert is_sensitive(grouped, include_pii=True) is True
    assert is_sensitive(ungrouped_long_number, include_pii=True) is False


def test_redact_replaces_secret_and_reports_findings():
    redacted, findings = redact('password = "mySecretPass123"')
    assert "mySecretPass123" not in redacted
    assert "REDACTED-HARDCODED_PASSWORD" in redacted
    assert len(findings) == 1


def test_redact_does_not_touch_pii_by_default():
    text = "contact: jane.doe@example.com"
    redacted, findings = redact(text)
    assert redacted == text
    assert findings == []


def test_scan_and_redact_chunk_text_respects_settings_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_KB_REPOS_ROOT", str(tmp_path / "repos"))
    monkeypatch.setenv("POSTGRES_ENABLED", "false")
    monkeypatch.setenv("SENSITIVE_SCAN_ENABLED", "false")
    settings = Settings(_env_file=None)
    text = 'password = "mySecretPass123"'
    redacted, findings = scan_and_redact_chunk_text(text, settings)
    assert redacted == text  # untouched when disabled
    assert findings == []

    monkeypatch.setenv("SENSITIVE_SCAN_ENABLED", "true")
    settings_enabled = Settings(_env_file=None)
    redacted2, findings2 = scan_and_redact_chunk_text(text, settings_enabled)
    assert redacted2 != text
    assert findings2
