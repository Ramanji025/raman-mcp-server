"""Repository Intelligence Engine package.

Public API surface::

    from mcp_kb.ingestion.repository_intelligence import (
        RepositoryIntelligenceEngine,
        ProjectKnowledgeModel,
        RepositoryType,
        ArchitectureStyle,
        TechStackModel,
    )
"""
from .arch_discovery import ArchitectureDiscoveryEngine
from .engine import RepositoryIntelligenceEngine
from .models import (
    ArchEvidence,
    ArchitectureModel,
    ArchitectureStyle,
    BuildSystem,
    DependencyModel,
    DeploymentModel,
    DeploymentTarget,
    FrameworkModel,
    IntegrationModel,
    ObservabilityModel,
    ParserSelectionModel,
    PrimaryLanguage,
    ProjectKnowledgeModel,
    RepositoryType,
    SecurityModel,
    TechEvidence,
    TechStackModel,
)
from .parser_selector import AdaptiveParserSelector
from .repo_classifier import RepositoryClassifier
from .tech_detector import TechnologyDetector

__all__ = [
    # Engine
    "RepositoryIntelligenceEngine",
    # Models
    "ProjectKnowledgeModel",
    "TechStackModel",
    "ArchitectureModel",
    "FrameworkModel",
    "DependencyModel",
    "DeploymentModel",
    "IntegrationModel",
    "ObservabilityModel",
    "SecurityModel",
    "ParserSelectionModel",
    "TechEvidence",
    "ArchEvidence",
    # Enums
    "RepositoryType",
    "ArchitectureStyle",
    "PrimaryLanguage",
    "BuildSystem",
    "DeploymentTarget",
    # Components (exposed for extension / testing)
    "TechnologyDetector",
    "ArchitectureDiscoveryEngine",
    "RepositoryClassifier",
    "AdaptiveParserSelector",
]
