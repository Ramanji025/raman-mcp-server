"""Repository Intelligence Engine — domain models.

These models represent a complete understanding of a repository *before* any
source-code parsing begins.  Every downstream parser receives a
``ProjectKnowledgeModel`` so it can enrich its output with pre-loaded
technology graph context rather than guessing from raw source alone.
"""
from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class RepositoryType(str, Enum):
    """Coarse classification of what a repository *is*."""
    MICROSERVICE       = "Microservice"
    MONOLITH           = "Monolith"
    LIBRARY            = "Library"
    SHARED_FRAMEWORK   = "SharedFramework"
    BATCH_APPLICATION  = "BatchApplication"
    EVENT_DRIVEN       = "EventDrivenService"
    FRONTEND           = "FrontendApplication"
    INFRASTRUCTURE     = "InfrastructureRepository"
    DOCUMENTATION      = "DocumentationRepository"
    GATEWAY            = "Gateway"
    AUTH_SERVICE       = "AuthenticationService"
    UTILITY            = "UtilityService"
    UNKNOWN            = "Unknown"


class ArchitectureStyle(str, Enum):
    """High-level architecture inferred from repo evidence."""
    LAYERED         = "Layered"
    CLEAN           = "CleanArchitecture"
    HEXAGONAL       = "HexagonalArchitecture"
    DDD             = "DomainDrivenDesign"
    CQRS            = "CQRS"
    EVENT_DRIVEN    = "EventDriven"
    MICROSERVICES   = "Microservices"
    SERVERLESS      = "Serverless"
    MONOLITHIC      = "Monolithic"
    PLUGIN_BASED    = "PluginBased"
    UNKNOWN         = "Unknown"


class BuildSystem(str, Enum):
    """Build/package-manager tooling detected for a repository."""

    MAVEN       = "Maven"
    GRADLE      = "Gradle"
    NPM         = "npm"
    YARN        = "Yarn"
    PNPM        = "pnpm"
    PIP         = "pip"
    POETRY      = "Poetry"
    DOTNET      = "dotnet"
    GO_MOD      = "GoModules"
    CARGO       = "Cargo"
    MAKEFILE    = "Makefile"
    BAZEL       = "Bazel"
    UNKNOWN     = "Unknown"


class PrimaryLanguage(str, Enum):
    """Dominant programming language detected for a repository."""

    JAVA        = "Java"
    CSHARP      = "CSharp"
    PYTHON      = "Python"
    TYPESCRIPT  = "TypeScript"
    JAVASCRIPT  = "JavaScript"
    GO          = "Go"
    RUST        = "Rust"
    KOTLIN      = "Kotlin"
    SCALA       = "Scala"
    RUBY        = "Ruby"
    UNKNOWN     = "Unknown"


class DeploymentTarget(str, Enum):
    """Deployment platform detected for a repository."""

    KUBERNETES  = "Kubernetes"
    DOCKER      = "Docker"
    HELM        = "Helm"
    TERRAFORM   = "Terraform"
    SERVERLESS  = "Serverless"
    BARE_METAL  = "BareMetal"
    UNKNOWN     = "Unknown"


# ---------------------------------------------------------------------------
# Evidence primitives
# ---------------------------------------------------------------------------

class TechEvidence(BaseModel):
    """A single signal that supports a technology detection conclusion."""
    signal: str                     # e.g. "pom.xml contains spring-boot-starter-web"
    source_file: str                # relative path that produced the signal
    confidence: float = 1.0         # 0.0–1.0
    technology: str = ""            # e.g. "Spring Boot"
    version: str | None = None      # detected version if available
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArchEvidence(BaseModel):
    """A single architectural signal."""
    signal: str                     # e.g. "package 'domain' found → DDD indicator"
    source_file: str
    style: ArchitectureStyle
    confidence: float = 1.0


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

class TechStackModel(BaseModel):
    """Everything we know about the technology stack of a repository."""

    primary_language: PrimaryLanguage = PrimaryLanguage.UNKNOWN
    build_systems: list[BuildSystem] = Field(default_factory=list)

    # Detected technology names (e.g. "Spring Boot", "Hibernate", "Kafka")
    frameworks: list[str] = Field(default_factory=list)
    libraries: list[str] = Field(default_factory=list)
    messaging_technologies: list[str] = Field(default_factory=list)
    persistence_technologies: list[str] = Field(default_factory=list)
    security_technologies: list[str] = Field(default_factory=list)
    test_frameworks: list[str] = Field(default_factory=list)
    cloud_platforms: list[str] = Field(default_factory=list)
    observability_components: list[str] = Field(default_factory=list)
    integrations: list[str] = Field(default_factory=list)

    # All raw evidence that led to these detections
    evidence: list[TechEvidence] = Field(default_factory=list)

    # Raw dependency declarations (groupId:artifactId / package@version / etc.)
    raw_dependencies: list[str] = Field(default_factory=list)

    # Configuration systems detected (Spring Cloud Config, Consul, Vault, etc.)
    config_systems: list[str] = Field(default_factory=list)

    # Documentation sources found (OpenAPI, Swagger, AsciiDoc, Javadoc, etc.)
    doc_sources: list[str] = Field(default_factory=list)


class ArchitectureModel(BaseModel):
    """Inferred architecture of the repository."""

    primary_style: ArchitectureStyle = ArchitectureStyle.UNKNOWN
    secondary_styles: list[ArchitectureStyle] = Field(default_factory=list)

    # Package/namespace structure evidence
    layer_packages: dict[str, list[str]] = Field(default_factory=dict)
    # e.g. {"controller": ["com.example.order.controller"], "service": [...]}

    # DDD indicators
    has_domain_layer: bool = False
    has_application_layer: bool = False
    has_infrastructure_layer: bool = False
    has_ports_adapters: bool = False

    # CQRS indicators
    has_command_handlers: bool = False
    has_query_handlers: bool = False
    has_event_sourcing: bool = False

    # Event-driven indicators
    event_driven_confidence: float = 0.0

    # Architecture evidence
    evidence: list[ArchEvidence] = Field(default_factory=list)
    confidence: float = 0.0


class FrameworkModel(BaseModel):
    """Detected frameworks with their detected capabilities."""

    name: str
    version: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    # e.g. Spring Boot → ["web", "security", "data-jpa", "actuator", "kafka"]
    config_properties: dict[str, str] = Field(default_factory=dict)
    # e.g. {"spring.application.name": "order-service"}
    annotations_used: list[str] = Field(default_factory=list)
    entry_points: list[str] = Field(default_factory=list)
    # e.g. Main class, @SpringBootApplication class


class DependencyModel(BaseModel):
    """All dependency declarations in the repository."""

    ecosystem: str                  # maven | npm | pip | nuget | go | cargo
    direct: list[str] = Field(default_factory=list)
    test_scoped: list[str] = Field(default_factory=list)
    provided: list[str] = Field(default_factory=list)
    # Parsed version constraints
    managed_versions: dict[str, str] = Field(default_factory=dict)
    # BOM / parent imports
    imported_boms: list[str] = Field(default_factory=list)


class DeploymentModel(BaseModel):
    """Deployment and infrastructure characteristics."""

    targets: list[DeploymentTarget] = Field(default_factory=list)
    has_dockerfile: bool = False
    has_docker_compose: bool = False
    has_helm_chart: bool = False
    has_kubernetes_manifests: bool = False
    has_terraform: bool = False
    has_serverless: bool = False
    container_base_images: list[str] = Field(default_factory=list)
    exposed_ports: list[int] = Field(default_factory=list)
    environment_variables: list[str] = Field(default_factory=list)
    helm_chart_name: str | None = None
    k8s_namespaces: list[str] = Field(default_factory=list)
    terraform_providers: list[str] = Field(default_factory=list)


class IntegrationModel(BaseModel):
    """External integrations detected."""

    external_services: list[str] = Field(default_factory=list)
    feign_clients: list[str] = Field(default_factory=list)
    rest_template_usages: list[str] = Field(default_factory=list)
    graphql_schemas: list[str] = Field(default_factory=list)
    grpc_definitions: list[str] = Field(default_factory=list)
    openapi_specs: list[str] = Field(default_factory=list)


class ObservabilityModel(BaseModel):
    """Observability characteristics."""

    has_actuator: bool = False
    has_micrometer: bool = False
    has_opentelemetry: bool = False
    has_zipkin: bool = False
    has_jaeger: bool = False
    has_prometheus: bool = False
    has_elk_stack: bool = False
    has_datadog: bool = False
    custom_metrics: list[str] = Field(default_factory=list)
    log_frameworks: list[str] = Field(default_factory=list)


class SecurityModel(BaseModel):
    """Security characteristics."""

    has_spring_security: bool = False
    has_oauth2: bool = False
    has_jwt: bool = False
    has_keycloak: bool = False
    has_vault: bool = False
    auth_mechanisms: list[str] = Field(default_factory=list)
    security_annotations: list[str] = Field(default_factory=list)


class ParserSelectionModel(BaseModel):
    """Which parsers should be activated for this repository, and in what order."""

    # content_type → parser class name (resolved by AdaptiveParserSelector)
    selected_parsers: dict[str, str] = Field(default_factory=dict)
    # Ordered parse priority (lower = parse first)
    parse_order: list[str] = Field(default_factory=list)
    # Additional content-type → glob patterns discovered at runtime
    dynamic_patterns: dict[str, list[str]] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Root model
# ---------------------------------------------------------------------------

class ProjectKnowledgeModel(BaseModel):
    """The complete pre-parse intelligence picture of a repository.

    This is the primary source of truth produced by the Repository
    Intelligence Engine.  It flows through the entire ingestion pipeline,
    enriching every parser and every graph node that is subsequently created.
    """

    repo_name: str
    repo_path: str
    repo_type: RepositoryType = RepositoryType.UNKNOWN
    repo_type_confidence: float = 0.0
    repo_type_evidence: list[str] = Field(default_factory=list)

    tech_stack: TechStackModel = Field(default_factory=TechStackModel)
    architecture: ArchitectureModel = Field(default_factory=ArchitectureModel)

    frameworks: list[FrameworkModel] = Field(default_factory=list)
    dependencies: list[DependencyModel] = Field(default_factory=list)
    deployment: DeploymentModel = Field(default_factory=DeploymentModel)
    integrations: IntegrationModel = Field(default_factory=IntegrationModel)
    observability: ObservabilityModel = Field(default_factory=ObservabilityModel)
    security: SecurityModel = Field(default_factory=SecurityModel)

    parser_selection: ParserSelectionModel = Field(default_factory=ParserSelectionModel)

    # Summary for logging / audit
    summary: str = ""
    analysed_at: datetime = Field(default_factory=_now)
    analysis_duration_ms: float = 0.0

    # Raw metadata preserved for graph node creation
    metadata: dict[str, Any] = Field(default_factory=dict)

    def as_node_attributes(self) -> dict[str, Any]:
        """Flatten key properties for use as a Neo4j graph node attribute bag."""
        return {
            "repo_type": self.repo_type.value,
            "primary_language": self.tech_stack.primary_language.value,
            "build_systems": [b.value for b in self.tech_stack.build_systems],
            "frameworks": self.tech_stack.frameworks,
            "architecture_style": self.architecture.primary_style.value,
            "deployment_targets": [d.value for d in self.deployment.targets],
            "repo_type_confidence": round(self.repo_type_confidence, 3),
            "summary": self.summary,
        }
