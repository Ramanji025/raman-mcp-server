"""LLM-specific observability via Langfuse (prompts, completions, cost, evals).

Complements ``observability/tracing.py`` (tool-level OTel spans/latency) with
generation-level detail that Phoenix/Langfuse specialize in: the exact
system/user prompt, the model's completion, token usage, and latency per LLM
call — not just aggregate tool timing.

Design goals (mirrors ``tracing.py``):
* **Zero-cost when disabled** (``LANGFUSE_ENABLED=false``, the default).
* **Graceful degradation**: if the ``langfuse`` package isn't installed, log
  one warning and continue with no Langfuse calls — never crash the server.
* **No call-site changes for callers who don't care**: :func:`log_generation`
  is a no-op until :func:`configure_langfuse` succeeds.
"""
from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from ..config import Settings
from ..logging import get_logger

log = get_logger(__name__)

_client: Any = None
_configured = False


def configure_langfuse(settings: Settings) -> None:
    """Idempotently initialize the Langfuse client, if enabled/available."""
    global _client, _configured
    if _configured:
        return
    _configured = True

    if not settings.langfuse_enabled:
        log.info("langfuse_disabled", hint="set LANGFUSE_ENABLED=true to enable LLM tracing")
        return

    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        log.warning("langfuse_missing_keys",
                    hint="set LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY to enable tracing")
        return

    try:
        from langfuse import Langfuse

        _client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
        log.info("langfuse_configured", host=settings.langfuse_host)
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on optional extra
        log.warning("langfuse_sdk_not_installed", error=str(exc),
                    hint="pip install langfuse")
        _client = None
    except Exception as exc:  # pragma: no cover - defensive, never crash the server on this
        log.warning("langfuse_setup_failed", error=str(exc))
        _client = None


@contextmanager
def trace_generation(
    name: str,
    *,
    model: str,
    system: str,
    user: str,
    metadata: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Context manager yielding a mutable dict; set ``result["completion"]``
    before exit to have it recorded alongside latency/errors."""
    start = time.perf_counter()
    outcome: dict[str, Any] = {"completion": "", "error": None}
    try:
        yield outcome
    except Exception as exc:
        outcome["error"] = str(exc)
        raise
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000
        _record(name, model=model, system=system, user=user,
                completion=outcome.get("completion", ""),
                elapsed_ms=elapsed_ms, error=outcome.get("error"),
                metadata=metadata)


def _record(
    name: str, *, model: str, system: str, user: str, completion: str,
    elapsed_ms: float, error: str | None, metadata: dict[str, Any] | None,
) -> None:
    log.debug("llm_generation", name=name, model=model, elapsed_ms=round(elapsed_ms, 2),
              error=bool(error), completion_chars=len(completion or ""))
    if _client is None:
        return
    try:
        generation = _client.start_observation(
            name=name,
            as_type="generation",
            model=model,
            input={"system": system, "user": user},
            metadata=metadata or {},
        )
        generation.update(output=completion, level="ERROR" if error else "DEFAULT",
                           status_message=error)
        generation.end()
    except Exception as exc:  # pragma: no cover - never break the LLM call path
        log.warning("langfuse_record_failed", error=str(exc))
