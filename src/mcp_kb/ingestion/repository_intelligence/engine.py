"""Repository Intelligence Engine — orchestrator.

This is the single entry point for all pre-parse repository intelligence.

Flow
----
1. TechnologyDetector   → DetectionContext  (what tech does this repo use?)
2. ArchitectureDiscoveryEngine → ArchitectureModel  (what architecture style?)
3. RepositoryClassifier → RepositoryType + confidence  (what *is* this repo?)
4. AdaptiveParserSelector → ParserSelectionModel  (which parsers to run?)
5. Assemble → ProjectKnowledgeModel  (the primary source of truth)

The ``ProjectKnowledgeModel`` is then passed to every parser and graph
builder so they can produce semantically enriched output.
"""
from __future__ import annotations

import time
from pathlib import Path

from ...config import Settings
from ...logging import get_logger
from ...models import EdgeType, GraphEdge, GraphNode, NodeType
from .arch_discovery import ArchitectureDiscoveryEngine
from .models import (
    ArchitectureModel,
    ProjectKnowledgeModel,
    RepositoryType,
    TechStackModel,
)
from .parser_selector import AdaptiveParserSelector
from .repo_classifier import RepositoryClassifier
from .tech_detector import TechnologyDetector

log = get_logger(__name__)


class RepositoryIntelligenceEngine:
    """Analyses a repository and returns a complete ``ProjectKnowledgeModel``.

    This model drives all downstream parsing decisions — parser selection
    and graph node creation.

    Usage::

        engine = RepositoryIntelligenceEngine(settings)
        model  = engine.analyse(repo_name="order-service",
                                repo_root=Path("/repos/order-service"))
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._tech_detector = TechnologyDetector()
        self._arch_engine = ArchitectureDiscoveryEngine()
        self._classifier = RepositoryClassifier()
        self._parser_selector = AdaptiveParserSelector()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def analyse(self, repo_name: str, repo_root: Path) -> ProjectKnowledgeModel:
        """Run the full intelligence pipeline for one repository.

        Returns a ``ProjectKnowledgeModel`` that is the primary source of
        truth for all downstream ingestion decisions.
        """
        start = time.perf_counter()
        log.info("rie_start", repo=repo_name, path=str(repo_root))

        # ── Step 1: Technology Detection ────────────────────────────────
        tech_ctx = self._tech_detector.detect(repo_root)

        # ── Step 2: Architecture Discovery ──────────────────────────────
        arch_model = self._arch_engine.discover(repo_root)

        # ── Step 3: Repository Classification ───────────────────────────
        repo_type, confidence, type_evidence = self._classifier.classify(
            repo_root, tech_ctx, arch_model.primary_style
        )

        # ── Step 4: Adaptive Parser Selection ───────────────────────────
        parser_selection = self._parser_selector.select(
            repo_root, tech_ctx.tech_stack, repo_type
        )

        # ── Step 5: Assemble ProjectKnowledgeModel ────────────────────
        elapsed_ms = (time.perf_counter() - start) * 1000

        model = ProjectKnowledgeModel(
            repo_name=repo_name,
            repo_path=str(repo_root),
            repo_type=repo_type,
            repo_type_confidence=confidence,
            repo_type_evidence=type_evidence,
            tech_stack=tech_ctx.tech_stack,
            architecture=arch_model,
            frameworks=tech_ctx.frameworks,
            dependencies=tech_ctx.dependencies,
            deployment=tech_ctx.deployment,
            integrations=tech_ctx.integrations,
            observability=tech_ctx.observability,
            security=tech_ctx.security,
            parser_selection=parser_selection,
            summary=_build_summary(repo_name, repo_type, tech_ctx.tech_stack, arch_model),
            analysis_duration_ms=round(elapsed_ms, 2),
        )

        log.info(
            "rie_complete",
            repo=repo_name,
            type=repo_type.value,
            confidence=confidence,
            language=tech_ctx.tech_stack.primary_language.value,
            architecture=arch_model.primary_style.value,
            frameworks=tech_ctx.tech_stack.frameworks,
            parsers=list(parser_selection.selected_parsers.keys()),
            duration_ms=round(elapsed_ms, 2),
        )
        return model

    # ------------------------------------------------------------------
    # Graph node factories
    # ------------------------------------------------------------------

    def to_graph_nodes(self, model: ProjectKnowledgeModel) -> tuple[list[GraphNode], list[GraphEdge]]:
        """Convert a ProjectKnowledgeModel to graph nodes + edges.

        Creates:
        - GIT_REPOSITORY node for the repo itself
        - TECHNOLOGY nodes for each detected technology
        - BELONGS_TO edges linking the repo to its technologies
        """
        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []

        # Repository node
        repo_node = GraphNode(
            id=f"repo:{model.repo_name}",
            type=NodeType.GIT_REPOSITORY,
            name=model.repo_name,
            service=model.repo_name,
            attributes={
                **model.as_node_attributes(),
                "repo_path": model.repo_path,
                "analysis_duration_ms": model.analysis_duration_ms,
                "analysed_at": model.analysed_at.isoformat(),
            },
        )
        nodes.append(repo_node)

        # Technology nodes
        all_techs = _gather_technologies(model.tech_stack)
        for tech in all_techs:
            tech_node_id = f"technology:{tech.lower().replace(' ', '_')}"
            tech_node = GraphNode(
                id=tech_node_id,
                type=NodeType.TECHNOLOGY,
                name=tech,
                attributes={"name": tech},
            )
            nodes.append(tech_node)
            edges.append(GraphEdge(
                src=repo_node.id,
                dst=tech_node_id,
                type=EdgeType.DEPENDS_ON,
                attributes={"relationship": "uses_technology"},
            ))

        # Architecture node
        arch_node_id = f"arch:{model.repo_name}:{model.architecture.primary_style.value}"
        arch_node = GraphNode(
            id=arch_node_id,
            type=NodeType.ARCHITECTURE,
            name=model.architecture.primary_style.value,
            service=model.repo_name,
            attributes={
                "style": model.architecture.primary_style.value,
                "confidence": model.architecture.confidence,
                "secondary_styles": [s.value for s in model.architecture.secondary_styles],
            },
        )
        nodes.append(arch_node)
        edges.append(GraphEdge(
            src=repo_node.id,
            dst=arch_node_id,
            type=EdgeType.IMPLEMENTS,
            attributes={"relationship": "implements_architecture"},
        ))

        return nodes, edges

    def close(self) -> None:
        """Close the underlying tech enricher's Neo4j connection."""
        self._enricher.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gather_technologies(ts: TechStackModel) -> list[str]:
    """Collect all distinct technology names from a TechStackModel."""
    seen: set[str] = set()
    result: list[str] = []
    for tech in (
        ts.frameworks
        + ts.libraries
        + ts.messaging_technologies
        + ts.persistence_technologies
        + ts.security_technologies
        + ts.test_frameworks
        + ts.cloud_platforms
        + ts.observability_components
    ):
        if tech and tech not in seen:
            seen.add(tech)
            result.append(tech)
    return result


def _build_summary(
    repo_name: str,
    repo_type: RepositoryType,
    ts: TechStackModel,
    arch: ArchitectureModel,
) -> str:
    lang = ts.primary_language.value
    frameworks = ", ".join(ts.frameworks[:5]) or "unknown"
    arch_style = arch.primary_style.value
    messaging = ", ".join(ts.messaging_technologies) or "none"
    persistence = ", ".join(ts.persistence_technologies[:3]) or "none"
    return (
        f"{repo_name} is a {repo_type.value} written in {lang}. "
        f"Frameworks: {frameworks}. Architecture: {arch_style}. "
        f"Messaging: {messaging}. Persistence: {persistence}."
    )
