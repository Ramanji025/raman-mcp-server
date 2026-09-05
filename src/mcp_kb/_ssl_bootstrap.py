"""Route TLS verification through the operating-system trust store.

On corporate networks that perform TLS inspection, an internal root CA is
injected into the certificate chain. Libraries that ship their own CA bundle
(``httpx``/``certifi`` used by huggingface_hub, openai, qdrant-client, etc.)
then fail with ``CERTIFICATE_VERIFY_FAILED``. ``truststore`` makes Python's
``ssl`` module use the OS trust store, where that corporate root CA already
lives, so certificates are still verified (not disabled).

Enabled by default; set ``MCP_KB_USE_OS_TRUSTSTORE=0`` to opt out.
"""
from __future__ import annotations

import os

_INJECTED = False


def inject() -> bool:
    """Inject the OS trust store into ``ssl`` once. Returns True if applied."""
    global _INJECTED
    if _INJECTED:
        return True
    if os.getenv("MCP_KB_USE_OS_TRUSTSTORE", "1").lower() in {"0", "false", "no"}:
        return False
    try:
        import truststore

        truststore.inject_into_ssl()
        _INJECTED = True
        return True
    except Exception:
        # truststore missing or unsupported platform: fall back to certifi.
        return False
