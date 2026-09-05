"""RAG synthesis: optional LLM answer generation over hybrid context."""
from __future__ import annotations

import concurrent.futures
from typing import Any

from ..config import Settings, get_settings
from ..logging import get_logger
from ..models import RetrievedChunk
from ..observability.langfuse_tracing import trace_generation
from ..security.prompt_guard import guard_chunk_text
from .hybrid_retriever import HybridResult

log = get_logger(__name__)

# P5.2: shared thread pool for non-blocking LLM calls, sized from LLM_MAX_CONCURRENCY
_LLM_EXECUTOR: concurrent.futures.ThreadPoolExecutor | None = None


def _get_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _LLM_EXECUTOR
    if _LLM_EXECUTOR is None:
        _LLM_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
            max_workers=get_settings().llm_max_concurrency, thread_name_prefix="mcp-llm"
        )
    return _LLM_EXECUTOR


class LLMClient:
    """Thin optional wrapper over an OpenAI-compatible chat model.

    Vendor independence (goal #10): ``llm_provider`` selects *how* we talk to
    a model, not *which* model — ``openai``/``azure`` for hosted APIs, or
    ``ollama`` for a fully local, self-hosted model (``LLM_MODEL``, e.g. ``gemma4:26b``)
    served through Ollama's OpenAI-compatible endpoint.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        provider = settings.llm_provider.lower()
        self._enabled = (
            (provider in ("openai", "azure") and bool(settings.openai_api_key))
            or provider == "ollama"
        )
        self._client = None
        if self._enabled:
            try:
                from openai import OpenAI

                if provider == "ollama":
                    self._client = OpenAI(
                        api_key=settings.openai_api_key or "ollama",
                        base_url=settings.ollama_base_url,
                        timeout=600.0,   # 26B models need time on complex prompts
                    )
                else:
                    self._client = OpenAI(
                        api_key=settings.openai_api_key,
                        base_url=settings.openai_base_url or None,
                        timeout=120.0,
                    )
            except Exception as exc:  # pragma: no cover
                log.warning("llm_init_failed", error=str(exc))
                self._enabled = False

    @property
    def enabled(self) -> bool:
        """Whether an LLM client was successfully initialized."""
        return self._enabled

    @property
    def settings(self) -> Settings:
        """The settings this client was configured with."""
        return self._settings

    def complete(self, system: str, user: str, temperature: float = 0.1) -> str:
        """Synchronously call the LLM with a system/user prompt; returns \"\" if disabled/failed."""
        if not self._enabled or self._client is None:
            return ""
        with trace_generation(
            "LLMClient.complete", model=self._settings.effective_llm_model, system=system,
            user=user, metadata={"provider": self._settings.llm_provider, "temperature": temperature},
        ) as outcome:
            extra: dict[str, Any] = {}
            if self._settings.llm_provider.lower() == "ollama":
                # thinking tokens are pure overhead for synthesis on a local server
                extra["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
            resp = self._client.chat.completions.create(
                model=self._settings.effective_llm_model,
                temperature=temperature,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                **extra,
            )
            completion = resp.choices[0].message.content or ""
            outcome["completion"] = completion
        return completion

    def complete_async(
        self, system: str, user: str, temperature: float = 0.1,
        timeout: float = 120.0,
    ) -> str:
        """P5.2: run the LLM call in a thread-pool worker so the server thread
        is not blocked. Falls back to empty string on timeout."""
        if not self._enabled or self._client is None:
            return ""
        future = _get_executor().submit(self.complete, system, user, temperature)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            log.warning("llm_complete_timeout", timeout=timeout)
            future.cancel()
            return ""
        except Exception as exc:
            log.warning("llm_complete_async_failed", error=str(exc))
            return ""


def build_context_block(
    chunks: list[RetrievedChunk], limit: int = 8, *, settings: Settings | None = None,
) -> str:
    """Render retrieved chunks into a numbered, citable context block.

    Each chunk's text is passed through the prompt-injection guardrail first
    (goal: ingested code/docs must not be able to hijack the synthesis LLM).
    """
    guard_enabled = settings.prompt_guard_enabled if settings is not None else True
    guard_mode = settings.prompt_guard_mode if settings is not None else "sanitize"
    lines: list[str] = []
    for i, rc in enumerate(chunks[:limit], start=1):
        c = rc.chunk
        loc = f"{c.repo}/{c.rel_path}"
        if c.start_line:
            loc += f":{c.start_line}"
        text = c.text.strip()[:1200]
        if guard_enabled:
            guarded, findings = guard_chunk_text(text, mode=guard_mode)
            if guarded is None:
                log.warning("chunk_dropped_by_prompt_guard", path=c.rel_path,
                            kinds=[f.kind for f in findings])
                continue
            text = guarded
        lines.append(f"[{i}] ({loc}) score={rc.score:.3f}\n{text}")
    return "\n\n".join(lines)


def citations_from(chunks: list[RetrievedChunk], limit: int = 8) -> list[dict[str, Any]]:
    """Build a numbered citation list (repo/path/line range) from retrieved chunks."""
    out = []
    for i, rc in enumerate(chunks[:limit], start=1):
        c = rc.chunk
        out.append({
            "ref": i, "repo": c.repo, "path": c.rel_path,
            "start_line": c.start_line, "end_line": c.end_line,
            "service": c.metadata.get("service"), "kind": c.metadata.get("kind"),
            "score": round(rc.score, 4),
        })
    return out


def synthesize(
    llm: LLMClient,
    question: str,
    result: HybridResult,
    *,
    system_prompt: str,
    fallback: str,
) -> str:
    """Return an LLM answer grounded in context, or ``fallback`` when no LLM."""
    if not llm.enabled or not result.chunks:
        return fallback
    context = build_context_block(result.chunks, settings=llm.settings)
    graph_summary = "\n".join(
        f"- {n.get('type')}: {n.get('name')} (service={n.get('service')})"
        for n in result.graph_nodes[:15]
    )
    user = (
        f"Question:\n{question}\n\n"
        f"Knowledge-graph facts:\n{graph_summary or '(none)'}\n\n"
        f"Retrieved context:\n{context}\n\n"
        "Answer using ONLY the facts and context above. Cite sources as [n]. "
        "If the context is insufficient, say so explicitly."
    )
    return llm.complete(system_prompt, user)


_PACK_SYSTEM = (
    "You are a knowledge-base formatter. Rephrase the evidence pack into a clear "
    "answer. Use ONLY facts in the pack. Cite sources already listed. "
    "If Gaps say evidence is insufficient, say so. Never invent service names, "
    "APIs, tables, Kafka topics, or root causes. Web snippets must not be used "
    "to name internal systems."
)


def synthesize_from_pack(
    llm: LLMClient,
    pack,
    *,
    system_prompt: str | None = None,
) -> str:
    """Polish an extractive pack; return the pack markdown if the LLM is off."""
    fallback = pack.as_markdown()
    if not llm.enabled:
        return fallback
    polished = llm.complete(system_prompt or _PACK_SYSTEM, fallback)
    return polished or fallback
