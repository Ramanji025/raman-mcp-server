"""Phase 5: token-efficiency benchmark (structured tools vs. raw file dump).

Mirrors codebase-memory-mcp's MEASURING_SAVINGS.md methodology, but as a
deterministic, CI-safe proxy rather than an LLM-judged experiment: LLM-based
A/B comparison is inherently flaky and costly to run on every commit, so
this benchmark instead measures the *token cost of the evidence itself* —
how many tokens would need to enter an LLM's context to answer a question
via structured tool output vs. via reading raw source files start to finish.
This captures the actual mechanism behind CBM's claimed savings (structured
retrieval touches far fewer bytes than exploratory file reads) without
depending on a live LLM.

Token counts are approximated at ~4 characters/token (a standard,
widely-used heuristic for English/code text; avoids adding a tokenizer
dependency just for a benchmark script) — documented here so the number is
never mistaken for an exact tokenizer count.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import Settings
from ..ingestion.repo_scanner import RepoScanner
from ..logging import get_logger

log = get_logger(__name__)

_CHARS_PER_TOKEN = 4


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def benchmark_token_efficiency(knowledge_service: Any, repo_name: str, repo_dir: Path,
                               settings: Settings) -> dict[str, Any]:
    """Compare structured-tool token cost vs. raw-file-dump token cost for
    answering the same question ("explain this service") against one repo.
    """
    # Baseline condition: dump every ingestible source file in the repo,
    # as a naive "read everything to find the answer" agent would.
    scanner = RepoScanner(settings)
    baseline_chars = 0
    baseline_files = 0
    for source in scanner.scan(repo_name, repo_dir):
        try:
            baseline_chars += len(Path(source.abs_path).read_text(encoding="utf-8", errors="ignore"))
            baseline_files += 1
        except OSError:
            continue
    baseline_tokens = _approx_tokens(" " * baseline_chars)

    # Structured condition: one targeted tool call.
    response = knowledge_service.explain_service(repo_name)
    structured_text = response.markdown or response.summary
    structured_tokens = _approx_tokens(structured_text)

    ratio = (baseline_tokens / structured_tokens) if structured_tokens else None
    result = {
        "repo": repo_name,
        "baseline_files_read": baseline_files,
        "baseline_tokens_approx": baseline_tokens,
        "structured_tokens_approx": structured_tokens,
        "token_ratio": round(ratio, 1) if ratio else None,
        "tokenizer": f"~{_CHARS_PER_TOKEN} chars/token heuristic (not an exact tokenizer)",
    }
    log.info("token_efficiency_benchmark", **result)
    return result
