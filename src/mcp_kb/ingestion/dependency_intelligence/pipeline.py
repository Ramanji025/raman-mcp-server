"""Dependency Intelligence pipeline.

Orchestrates:
  1. Scan Maven pom.xml  → DependencyArtifact list
  2. Scan NuGet .csproj  → DependencyArtifact list
  3. Scan pip requirements.txt / pyproject.toml  → DependencyArtifact list
  4. Merge & deduplicate across ecosystems
  5. Resolve transitive dependencies within the workspace
  6. Write all results to Neo4j
"""
from __future__ import annotations

from pathlib import Path

from ...config import Settings
from ...logging import get_logger
from ...models import DependencyArtifact
from . import nuget_scanner, pom_scanner, requirements_scanner
from .graph_writer import DependencyGraphWriter
from .transitive_resolver import resolve as resolve_transitive

log = get_logger(__name__)


class DependencyIntelligencePipeline:
    """Scans repos for dependency manifests and maps artifacts to capability features."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or Settings()
        self._repos_root = Path(self._settings.repos_root)

    # ------------------------------------------------------------------ #

    def run(
        self,
        *,
        skip_maven: bool = False,
        skip_nuget: bool = False,
        skip_pip: bool = False,
        skip_transitive: bool = False,
        skip_graph: bool = False,
    ) -> dict:
        """Run the full pipeline and return a summary dict."""
        artifacts: list[DependencyArtifact] = []

        if not skip_maven:
            mvn = pom_scanner.scan(self._repos_root)
            log.info("dep_scan_maven", count=len(mvn))
            artifacts.extend(mvn)

        if not skip_nuget:
            nuget = nuget_scanner.scan(self._repos_root)
            log.info("dep_scan_nuget", count=len(nuget))
            artifacts.extend(nuget)

        if not skip_pip:
            pip = requirements_scanner.scan(self._repos_root)
            log.info("dep_scan_pip", count=len(pip))
            artifacts.extend(pip)

        # Merge duplicates across ecosystems (same id = same artifact)
        merged = _merge(artifacts)
        log.info("dep_scan_merged", unique_artifacts=len(merged))

        transitive_edges: list[tuple[str, str]] = []
        if not skip_transitive:
            transitive_edges = resolve_transitive(self._repos_root, merged)
            log.info("dep_transitive_resolved", edges=len(transitive_edges))

        nodes_written = edges_written = 0
        if not skip_graph:
            writer = DependencyGraphWriter()
            nodes_written, edges_written = writer.write(merged, transitive_edges)

        summary = {
            "total_artifacts": len(merged),
            "maven": sum(1 for d in merged if d.ecosystem == "maven"),
            "nuget": sum(1 for d in merged if d.ecosystem == "nuget"),
            "pip": sum(1 for d in merged if d.ecosystem == "pip"),
            "spring_starters": sum(1 for d in merged if d.is_spring_starter),
            "internal_artifacts": sum(1 for d in merged if d.is_internal),
            "with_features": sum(1 for d in merged if d.features),
            "transitive_edges": len(transitive_edges),
            "nodes_written": nodes_written,
            "edges_written": edges_written,
        }
        log.info("dep_intel_pipeline_done", **summary)
        return summary


def _merge(artifacts: list[DependencyArtifact]) -> list[DependencyArtifact]:
    """Merge entries with the same id, unioning services and features."""
    seen: dict[str, DependencyArtifact] = {}
    for da in artifacts:
        if da.id not in seen:
            seen[da.id] = da
        else:
            existing = seen[da.id]
            merged_services = list(dict.fromkeys(existing.services + da.services))
            merged_features = list(dict.fromkeys(existing.features + da.features))
            merged_concepts = list(dict.fromkeys(existing.concept_names + da.concept_names))
            seen[da.id] = existing.model_copy(update={
                "services": merged_services,
                "features": merged_features,
                "concept_names": merged_concepts,
            })
    return list(seen.values())
