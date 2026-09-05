"""End-to-end ingestion orchestration.

Ties git -> scan -> parse -> (vector index + knowledge graph + metadata)
together, with full and incremental (change-detection) modes (goals #4, #8).
"""
from __future__ import annotations

import argparse
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings, get_settings
from ..db.metadata_store import MetadataStore
from ..graph.enterprise_dependency_graph import EnterpriseDependencyGraphAugmenter
from ..graph.factory import get_graph_store
from ..knowledge.builder import KnowledgeBuilder
from ..knowledge.store import KnowledgeStore
from ..logging import get_logger
from ..models import ParseResult
from ..semantic import SemanticSummaryBuilder, SemanticSummaryCache
from ..vector.indexer import VectorIndexer
from .git_manager import GitManager, RepoChange
from .parsers import ParserRegistry
from .repo_scanner import RepoScanner
from .repository_intelligence import RepositoryIntelligenceEngine

log = get_logger(__name__)


def _rmtree_force(path: Path) -> None:
    """Remove a directory tree, handling Windows read-only and locked .git files."""
    if sys.platform == "win32":
        # 'rd /s /q' releases Git pack-file locks that shutil.rmtree cannot clear
        subprocess.run(["cmd", "/c", "rd", "/s", "/q", str(path)], check=True)
    else:
        def _clear_readonly(func, p, _):
            Path(p).chmod(stat.S_IWRITE)
            func(p)
        shutil.rmtree(path, onexc=_clear_readonly)


@dataclass
class IngestReport:
    """Summary of files/nodes/edges processed for one repo ingestion run."""

    repo: str
    mode: str
    files_indexed: int = 0
    files_deleted: int = 0
    knowledge_objects_written: int = 0
    chunks_written: int = 0
    nodes: int = 0
    edges: int = 0
    repo_type: str = ""
    architecture: str = ""
    primary_language: str = ""

    def as_dict(self) -> dict:
        """Return this project profile as a plain dict."""
        return self.__dict__


class IngestionPipeline:
    """Coordinates all ingestion components for one or many repositories."""

    def __init__(self, settings: Settings | None = None, *, generate_embeddings: bool = False) -> None:
        self.settings = settings or get_settings()
        self.generate_embeddings = generate_embeddings
        self.git = GitManager(self.settings)
        self.scanner = RepoScanner(self.settings)
        self.parsers = ParserRegistry(self.settings)
        self.indexer = VectorIndexer(self.settings) if self.generate_embeddings else None
        self.dep_graph = EnterpriseDependencyGraphAugmenter(self.settings)
        self.knowledge_builder = KnowledgeBuilder()
        self.knowledge_store = KnowledgeStore(self.settings)
        self.semantic_cache = SemanticSummaryCache(self.settings)
        self.semantic_builder = SemanticSummaryBuilder(self.settings, self.semantic_cache)
        self.graph = get_graph_store(self.settings)
        self.graph.load()  # continue from a prior snapshot (no-op for Neo4j)
        self.meta = MetadataStore(self.settings)
        self.rie = RepositoryIntelligenceEngine(self.settings)

    # ------------------------------------------------------------------ #
    # Public entry points
    # ------------------------------------------------------------------ #
    def clone_all(self) -> None:
        """Clone/pull every repository listed in the manifest, in parallel.

        Each repo lives in its own directory and uses its own git subprocess,
        so this is safe to parallelize — with 20-30+ repos this was previously
        the single largest sequential I/O wait in a full rebuild.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        entries = self.git.load_manifest()
        max_workers = min(8, max(1, len(entries)))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(self.git.clone_or_pull, e["name"], e["url"], e["branch"]): e["name"]
                for e in entries
            }
            for fut in as_completed(futures):
                name = futures[fut]
                try:
                    fut.result()
                except Exception as exc:
                    log.error("clone_failed", repo=name, error=str(exc))

    def ingest_all(self, incremental: bool = False) -> list[IngestReport]:
        """Ingest every locally-cloned repo (full or incremental) and save the graph."""
        repos = self.git.discover_local_repos()
        if not repos:
            log.warning("no_local_repos", root=str(self.settings.repos_root))
        reports = [self.ingest_repo(name, incremental=incremental) for name in repos]
        self.graph.save()
        self.meta.close()
        return reports

    def refresh_all(self) -> list[IngestReport]:
        """Git pull each repo then incrementally re-index only changes (goal #8)."""
        reports: list[IngestReport] = []
        for name in self.git.discover_local_repos():
            change = self.git.pull(name)
            reports.append(self._ingest_change(name, change))
        self.graph.save()
        self.meta.close()
        return reports

    def purge_and_refresh_all(self, repo: str | None = None) -> list[IngestReport]:
        """Delete local clone(s), re-clone from manifest, then incremental ingest."""
        manifest = {e["name"]: e for e in self.git.load_manifest()}
        names = [repo] if repo else list(manifest.keys())
        reports: list[IngestReport] = []
        for name in names:
            repo_dir = self.settings.repos_root / name
            if repo_dir.exists():
                log.info("purge_repo", repo=name, path=str(repo_dir))
                _rmtree_force(repo_dir)
            entry = manifest.get(name)
            if not entry:
                log.error("repo_not_in_manifest", repo=name)
                continue
            self.git.clone_or_pull(name, entry["url"], entry["branch"])
            last = self.meta.get_repo_commit(name)
            change = self.git.diff_against(name, last)
            reports.append(self._ingest_change(name, change))
        self.graph.save()
        self.meta.close()
        return reports

    def ingest_repo(self, name: str, incremental: bool = False) -> IngestReport:
        """Ingest one repo (full or incremental) and return its report."""
        repo_dir = self.settings.repos_root / name
        if incremental:
            last = self.meta.get_repo_commit(name)
            change = self.git.diff_against(name, last)
            return self._ingest_change(name, change)
        return self._full_ingest(name, repo_dir)

    # ------------------------------------------------------------------ #
    # Full vs incremental
    # ------------------------------------------------------------------ #
    def _full_ingest(self, name: str, repo_dir: Path) -> IngestReport:
        log.info("ingest_full", repo=name)
        report = IngestReport(repo=name, mode="full")

        # ── Repository Intelligence: analyse before any parsing ──────────
        project_model = self.rie.analyse(repo_name=name, repo_root=repo_dir)
        rie_nodes, rie_edges = self.rie.to_graph_nodes(project_model)
        self.graph.add_many(rie_nodes, rie_edges)
        log.info(
            "rie_nodes_written",
            repo=name,
            nodes=len(rie_nodes),
            edges=len(rie_edges),
            type=project_model.repo_type.value,
        )
        report.repo_type = project_model.repo_type.value
        report.architecture = project_model.architecture.primary_style.value
        report.primary_language = project_model.tech_stack.primary_language.value

        # ── Broadcast project model to all parsers ───────────────────────
        self.parsers.set_project_model(project_model)

        aggregate = ParseResult()
        source_paths: list[str] = []
        for source in self.scanner.scan(name, repo_dir):
            parsed = self.parsers.parse(source)
            aggregate.extend(parsed)
            source_paths.append(source.rel_path)
            report.files_indexed += 1
            self.meta.record_file(name, source.rel_path, source.content_type.value,
                                  source.sha256, len(parsed.chunks))
        self._commit(name, aggregate, source_paths, report)
        commit = self.git.current_commit(name)
        if commit:
            self.meta.set_repo_commit(name, commit)
        return report

    def _ingest_change(self, name: str, change: RepoChange) -> IngestReport:
        if change.is_full or not self.meta.known_hashes(name):
            return self._full_ingest(name, self.settings.repos_root / name)

        log.info("ingest_incremental", repo=name,
                 touched=len(change.touched), deleted=len(change.deleted))
        report = IngestReport(repo=name, mode="incremental")
        repo_dir = self.settings.repos_root / name
        known = self.meta.known_hashes(name)

        # ── Repository Intelligence (incremental also benefits from enrichment) ─
        project_model = self.rie.analyse(repo_name=name, repo_root=repo_dir)
        report.repo_type = project_model.repo_type.value
        report.architecture = project_model.architecture.primary_style.value
        report.primary_language = project_model.tech_stack.primary_language.value
        self.parsers.set_project_model(project_model)

        # Deletions: purge vectors + graph nodes tied to removed files.
        if change.deleted:
            if self.indexer is not None:
                self.indexer.remove_files(name, change.deleted)
            self.knowledge_store.delete_by_paths(name, change.deleted)
            for rel in change.deleted:
                self.graph.remove_service_file(name, rel)
            self.meta.forget_files(name, change.deleted)
            report.files_deleted = len(change.deleted)
            # P3.4: verify deleted files are no longer tracked in metadata
            still_known = self.meta.known_hashes(name)
            leaked = [r for r in change.deleted if r in still_known]
            if leaked:
                log.warning("incremental_delete_leak",
                            repo=name, count=len(leaked), sample=leaked[:3])

        aggregate = ParseResult()
        changed_paths: list[str] = []
        for source in self.scanner.scan_paths(name, repo_dir, change.touched):
            if known.get(source.rel_path) == source.sha256:
                continue  # content identical -> skip re-embedding
            changed_paths.append(source.rel_path)
            self.graph.remove_service_file(name, source.rel_path)  # replace stale nodes
            parsed = self.parsers.parse(source)
            aggregate.extend(parsed)
            report.files_indexed += 1
            self.meta.record_file(name, source.rel_path, source.content_type.value,
                                  source.sha256, len(parsed.chunks))

        # Replace vectors for changed files before re-inserting.
        if changed_paths and self.indexer is not None:
            self.indexer.remove_files(name, changed_paths)
        if changed_paths:
            self.knowledge_store.delete_by_paths(name, changed_paths)
        self._commit(name, aggregate, changed_paths, report)
        self.meta.set_repo_commit(name, change.new_commit)
        return report

    def _commit(self, name: str, parsed: ParseResult, source_paths: list[str], report: IngestReport) -> None:
        # Knowledge-first ingestion: persist semantic graph and typed objects first.
        augmented = self.dep_graph.augment(name, parsed)
        semantic_chunks = self.semantic_builder.build(augmented)
        augmented.chunks.extend(semantic_chunks)

        self.graph.add_many(augmented.nodes, augmented.edges)
        knowledge_objects = self.knowledge_builder.build(name, augmented, source_paths)
        report.knowledge_objects_written = self.knowledge_store.upsert_many(knowledge_objects)

        # Embeddings are optional and run after knowledge extraction is complete.
        report.chunks_written = self.indexer.index(augmented.chunks) if self.indexer is not None else 0
        report.nodes = len(augmented.nodes)
        report.edges = len(augmented.edges)
        log.info("repo_ingested", **report.as_dict())


# --------------------------------------------------------------------------- #
# CLI entry points (exposed via pyproject [project.scripts])
# --------------------------------------------------------------------------- #
def cli_main() -> None:
    """CLI entrypoint: clone/ingest microservice repositories."""
    parser = argparse.ArgumentParser(description="Ingest microservice repositories")
    parser.add_argument("--clone", action="store_true", help="clone/pull from manifest")
    parser.add_argument("--incremental", action="store_true",
                        help="index only files changed since last run")
    parser.add_argument("--with-embeddings", action="store_true",
                        help="run embedding generation after knowledge extraction")
    parser.add_argument("--refresh", action="store_true",
                        help="purge local clone, re-clone from git, incremental ingest")
    parser.add_argument("--repo", help="ingest a single repository by name")
    args = parser.parse_args()

    pipeline = IngestionPipeline(generate_embeddings=args.with_embeddings)
    if args.refresh:
        _print_reports(pipeline.purge_and_refresh_all(repo=args.repo))
        return
    if args.clone:
        pipeline.clone_all()
    if args.repo:
        report = pipeline.ingest_repo(args.repo, incremental=args.incremental)
        pipeline.graph.save()
        pipeline.meta.close()
        reports = [report]
    else:
        reports = pipeline.ingest_all(incremental=args.incremental)

    _print_reports(reports)


def cli_refresh() -> None:
    """`mcp-kb-refresh`: git pull + incremental re-index across all repos."""
    pipeline = IngestionPipeline(generate_embeddings=False)
    _print_reports(pipeline.refresh_all())


def _print_reports(reports: list[IngestReport]) -> None:
    from rich.console import Console
    from rich.table import Table

    table = Table(title="Ingestion summary")
    for col in ("repo", "mode", "type", "language", "architecture",
                "files", "deleted", "knowledge", "chunks", "nodes", "edges"):
        table.add_column(col)
    for r in reports:
        table.add_row(r.repo, r.mode, r.repo_type, r.primary_language, r.architecture,
                      str(r.files_indexed), str(r.files_deleted),
                      str(r.knowledge_objects_written), str(r.chunks_written),
                      str(r.nodes), str(r.edges))
    Console().print(table)


if __name__ == "__main__":
    cli_main()
