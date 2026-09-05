"""Sensitive-code and PII detection for ingested content.

Deterministic, regex-based (not LLM-based) so results are reproducible and
auditable. Used two ways:

1. **Ingestion guard** (``VectorIndexer``): chunks containing likely secrets
   are redacted before being embedded/stored, so credentials never leak into
   the vector index or get echoed back in a tool response.
2. **Ad-hoc scanning**: any tool/script can call :func:`scan_text` directly,
   e.g. to flag a pasted stack trace or log blob before it's persisted as an
   incident.

Patterns are intentionally conservative (favouring recall over precision for
credential-shaped strings) since the cost of over-redacting a false positive
in a code chunk is low, while leaking a real secret is not.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import Settings
from ..logging import get_logger

log = get_logger(__name__)


@dataclass
class SensitiveFinding:
    """One detected secret/PII span (API key, password, etc.) found by the scanner."""

    kind: str
    start: int
    end: int
    snippet: str


_SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("aws_secret_key", re.compile(r"(?i)aws_secret_access_key\s*[=:]\s*['\"]?[A-Za-z0-9/+=]{40}")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("generic_api_key", re.compile(r"(?i)\b(?:api[_-]?key|apikey|access[_-]?token)\b\s*[=:]\s*"
                                   r"['\"]?[A-Za-z0-9_\-]{16,}")),
    ("hardcoded_password", re.compile(r"(?i)\bpassword\b\s*[=:]\s*['\"][^'\"\s]{4,}['\"]")),
    ("jdbc_credentials", re.compile(r"jdbc:[a-z]+://[^\s'\"]*[?&]password=[^&\s'\"]+")),
    ("slack_webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]+")),
    ("jwt_token", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
]

# PII patterns are opt-in-only (via include_pii=True) — never auto-redacted
# during bulk code ingestion, since these are far more prone to false
# positives on ordinary source content:
#   - email_address matches legitimate @author tags / pom.xml <developers>
#   - credit_card (even restricted to an explicit 4-4-4-4 grouping) and ssn
#     can still coincidentally match version strings, IDs, or test fixtures.
# They remain fully available for explicit, human-directed scans (e.g.
# scanning a pasted stack trace/log blob before persisting an incident).
_PII_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("email_address", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    # Requires explicit 4-4-4-4 grouping (space or dash separated) to avoid
    # matching arbitrary long digit runs (line numbers, hashes, IDs) common
    # in real source code.
    ("credit_card", re.compile(r"\b\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{4}\b")),
]

_PATTERNS: list[tuple[str, re.Pattern]] = _SECRET_PATTERNS + _PII_PATTERNS

# Emails are common and low-severity in a code KB (e.g. @author tags,
# pom.xml <developers>); flag them as PII but don't redact by default.
_PII_ONLY_KINDS = {kind for kind, _ in _PII_PATTERNS}


def scan_text(text: str) -> list[SensitiveFinding]:
    """Return every likely secret/PII match in ``text``."""
    findings: list[SensitiveFinding] = []
    for kind, pattern in _PATTERNS:
        for m in pattern.finditer(text):
            findings.append(SensitiveFinding(kind=kind, start=m.start(), end=m.end(),
                                             snippet=m.group(0)[:60]))
    return findings


def is_sensitive(text: str, *, include_pii: bool = False) -> bool:
    """Return True if `text` contains a secret (or PII, when `include_pii` is set)."""
    findings = scan_text(text)
    if include_pii:
        return bool(findings)
    return any(f.kind not in _PII_ONLY_KINDS for f in findings)


def redact(text: str, *, include_pii: bool = False) -> tuple[str, list[SensitiveFinding]]:
    """Replace every secret (and optionally PII) match with a redaction marker.

    Returns the redacted text plus the findings that triggered redaction, so
    callers can log/audit what was removed without persisting the value
    itself.
    """
    findings = scan_text(text)
    to_redact = [f for f in findings if include_pii or f.kind not in _PII_ONLY_KINDS]
    if not to_redact:
        return text, []
    # Redact from the end so earlier offsets stay valid as we mutate the string.
    out = text
    for f in sorted(to_redact, key=lambda f: f.start, reverse=True):
        out = out[: f.start] + f"***REDACTED-{f.kind.upper()}***" + out[f.end :]
    return out, to_redact


def scan_and_redact_chunk_text(text: str, settings: Settings) -> tuple[str, list[SensitiveFinding]]:
    """Ingestion-time hook: no-op when ``SENSITIVE_SCAN_ENABLED=false``."""
    if not settings.sensitive_scan_enabled:
        return text, []
    redacted, findings = redact(text)
    if findings:
        log.warning("sensitive_content_redacted",
                     kinds=sorted({f.kind for f in findings}), count=len(findings))
    return redacted, findings
