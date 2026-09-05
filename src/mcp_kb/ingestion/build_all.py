"""Full platform build — runs the complete knowledge pipeline end-to-end.

This is the single command that executes ALL stages in correct order:

  Stage 1  Repository Intelligence     (RepositoryIntelligenceEngine → per-repo)
  Stage 2  Project Parser              (ParserRegistry → graph nodes + chunks)
  Stage 3  Project Knowledge Graph     (KnowledgeBuilder → Neo4j)
  Stage 4  Incident Knowledge Graph    (MarkdownParser incidents → Neo4j)
  Stage 5  Dependency Intelligence     (DependencyIntelligencePipeline)
  Stage 6  Vector Embeddings           (VectorIndexer → Qdrant)
  Stage 7  LLM Semantic Enrichment     (Ollama → semantic summaries)

Usage
-----
  # Full rebuild from scratch
  mcp-kb-build-all

  # No LLM (offline / CI mode)
  mcp-kb-build-all --skip-llm

  # Single repo only
  mcp-kb-build-all --repo order-service
"""
from __future__ import annotations

import argparse
import sys
import time

from ..config import get_settings
from ..logging import get_logger
from .full_rewrite import _check_ollama, _run_llm_enrichment
from .pipeline import IngestionPipeline, _print_reports

log = get_logger(__name__)

_W = 68


def _banner(stage: str, title: str) -> None:
    bar = "─" * _W
    print(f"\n┌{bar}┐")
    print(f"│  {stage:<8} {title:<{_W - 11}}│")
    print(f"└{bar}┘")


def _section(title: str) -> None:
    print(f"\n{'═' * _W}")
    print(f"  {title}")
    print(f"{'═' * _W}")


def _run_project_knowledge(args: argparse.Namespace) -> bool:
    """Run Stages 1-7 (repo intelligence → project graph → embeddings → LLM)."""
    _section("Project Knowledge Pipeline")

    _banner("Stage 1-4", "Repository Intelligence → Project Knowledge Graph")

    generate_embeddings = not args.skip_embeddings
    pipeline = IngestionPipeline(generate_embeddings=generate_embeddings)

    if not args.skip_clone:
        log.info("clone_start")
        pipeline.clone_all()

    if args.repo:
        report = pipeline.ingest_repo(args.repo, incremental=True)
        pipeline.graph.save()
        pipeline.meta.close()
        reports = [report]
    else:
        reports = pipeline.ingest_all(incremental=True)

    _print_reports(reports)

    # Stage 5: Dependency Intelligence
    if not args.skip_dependencies:
        _banner("Stage 5/7", "Dependency Intelligence")
        from .dependency_intelligence.pipeline import DependencyIntelligencePipeline

        dp = DependencyIntelligencePipeline()
        try:
            summary = dp.run()
            print(f"  Artifacts: {summary['total_artifacts']} | "
                  f"Nodes: {summary['nodes_written']} | "
                  f"Edges: {summary['edges_written']}")
        except KeyboardInterrupt:
            print("\nDependency intelligence interrupted.")
            return False
    else:
        print("Stage 5 skipped (--skip-dependencies).")

    # Stage 7: LLM Semantic Enrichment
    if args.skip_llm:
        print("Stage 7 skipped (--skip-llm).")
        return True

    if not _check_ollama(args.ollama_url):
        msg = f"Ollama not reachable at {args.ollama_url}. Pass --skip-llm to bypass."
        if args.allow_llm_failure:
            log.warning("ollama_unavailable_skipping", reason=msg)
            return True
        raise SystemExit(msg)

    _banner("Stage 7/7", "LLM Semantic Enrichment (Ollama)")
    code = _run_llm_enrichment(pilot=args.pilot_llm)
    if code != 0 and not args.allow_llm_failure:
        raise SystemExit(code)

    from ..semantic.enrichment_sync import sync_ollama_enrichment
    synced = sync_ollama_enrichment()
    log.info("llm_enrichment_done", vectors_synced=synced)
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cli_build_all() -> None:
    """Entry point for ``mcp-kb-build-all``."""
    parser = argparse.ArgumentParser(
        description="Full platform build: Project Knowledge → Embeddings → LLM enrichment"
    )

    parser.add_argument("--repo", help="Limit the build to a single repository name.")
    parser.add_argument("--skip-clone",        action="store_true", help="Skip git clone/pull.")
    parser.add_argument("--skip-dependencies", action="store_true", help="Skip Stage 5: dependency intelligence.")
    parser.add_argument("--skip-embeddings",   action="store_true", help="Skip vector embedding generation.")
    parser.add_argument("--skip-llm",          action="store_true", help="Skip Stage 7: LLM enrichment.")
    parser.add_argument("--pilot-llm",         action="store_true", help="Use pilot LLM script instead of full.")
    parser.add_argument("--allow-llm-failure", action="store_true", help="Don't fail on LLM errors.")
    parser.add_argument(
        "--llm-workers", type=int, default=None,
        help="Parallel LLM threads (0=no LLM; default: LLM_MAX_CONCURRENCY from .env).",
    )
    parser.add_argument(
        "--ollama-url", default=get_settings().ollama_base_url,
        help="OpenAI-compatible LLM base URL used for health check (default: OLLAMA_BASE_URL from .env).",
    )

    args = parser.parse_args()

    # Resolve --llm-workers default from LLM_MAX_CONCURRENCY (.env) when unset
    if args.llm_workers is None:
        args.llm_workers = get_settings().llm_max_concurrency

    # Resolve --skip-llm when llm-workers == 0
    if args.llm_workers == 0:
        args.skip_llm = True

    total_start = time.monotonic()
    success = _run_project_knowledge(args)

    elapsed = time.monotonic() - total_start
    status = "✅ complete" if success else "⚠️  completed with errors"
    print(f"\n{status} in {elapsed:.1f}s")
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    cli_build_all()
