"""One-command full platform rewrite pipeline.

Runs clone/pull, full ingestion, optional embeddings, and optional local-LLM
full enrichment in a single command for enterprise knowledge rebuilds.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import requests

from ..config import get_settings
from ..logging import get_logger
from ..semantic.enrichment_sync import sync_ollama_enrichment
from .pipeline import IngestionPipeline, _print_reports

log = get_logger(__name__)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _check_ollama(base_url: str, timeout: int = 5) -> bool:
    # /v1/models is served by both llama.cpp and Ollama; /api/tags is Ollama-only
    try:
        resp = requests.get(f"{base_url.rstrip('/')}/models", timeout=timeout)
        return resp.status_code == 200
    except Exception:
        return False


def _run_llm_enrichment(*, pilot: bool = False) -> int:
    repo_root = _repo_root()
    script = repo_root / "scripts" / ("llm_enrich_pilot.py" if pilot else "llm_enrich_full.py")
    if not script.exists():
        log.error("llm_enrichment_script_missing", script=str(script))
        return 2
    cmd = [sys.executable, str(script)]
    log.info("llm_enrichment_start", script=str(script))
    proc = subprocess.run(cmd, cwd=str(repo_root), check=False)
    if proc.returncode != 0:
        log.error("llm_enrichment_failed", code=proc.returncode)
    else:
        log.info("llm_enrichment_done", script=str(script))
    return proc.returncode


def cli_rewrite() -> None:
    """CLI entrypoint: clone/pull -> full ingest -> embeddings -> local LLM enrichment."""
    parser = argparse.ArgumentParser(
        description=(
            "Full platform rewrite: clone/pull -> full ingest -> embeddings -> local LLM enrichment"
        )
    )
    parser.add_argument("--repo", help="ingest a single repository by name")
    parser.add_argument(
        "--skip-clone",
        action="store_true",
        help="skip clone/pull step and use existing local checkouts",
    )
    parser.add_argument(
        "--skip-embeddings",
        action="store_true",
        help="skip embedding generation (knowledge graph + objects still built)",
    )
    parser.add_argument(
        "--skip-llm-enrichment",
        action="store_true",
        help="skip scripts/llm_enrich_full.py execution",
    )
    parser.add_argument(
        "--pilot-llm",
        action="store_true",
        help="run scripts/llm_enrich_pilot.py instead of full enrichment",
    )
    parser.add_argument(
        "--ollama-url",
        default=get_settings().ollama_base_url,
        help="OpenAI-compatible LLM base URL used for health check before enrichment",
    )
    parser.add_argument(
        "--allow-llm-failure",
        action="store_true",
        help="do not fail command if enrichment fails",
    )
    args = parser.parse_args()

    pipeline = IngestionPipeline(generate_embeddings=not args.skip_embeddings)

    if not args.skip_clone:
        log.info("rewrite_clone_start")
        pipeline.clone_all()

    if args.repo:
        report = pipeline.ingest_repo(args.repo, incremental=False)
        pipeline.graph.save()
        pipeline.meta.close()
        reports = [report]
    else:
        reports = pipeline.ingest_all(incremental=False)

    _print_reports(reports)

    if args.skip_llm_enrichment:
        log.info("rewrite_done", llm_enrichment="skipped")
        return

    if not _check_ollama(args.ollama_url):
        msg = (
            "Ollama is not reachable. Start Ollama or pass --skip-llm-enrichment. "
            f"Checked: {args.ollama_url}"
        )
        if args.allow_llm_failure:
            log.warning("ollama_unavailable", url=args.ollama_url)
            log.warning("llm_enrichment_skipped", reason=msg)
            return
        raise SystemExit(msg)

    code = _run_llm_enrichment(pilot=args.pilot_llm)
    if code != 0:
        if not args.allow_llm_failure:
            raise SystemExit(code)
        return

    synced = sync_ollama_enrichment()
    log.info("rewrite_done", ollama_enrichment_vectors=synced)


if __name__ == "__main__":
    cli_rewrite()
