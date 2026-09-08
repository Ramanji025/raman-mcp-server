"""Shared domain models used across ingestion, graph, vector and tools layers."""
from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, TypedDict

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# Graph traversal result shapes (formalizes ad-hoc dicts returned by
# GraphStorePort.impacted() and KnowledgeService._find_callers()).
# --------------------------------------------------------------------------- #
class ImpactResult(TypedDict):
    """Reverse-reachability result from GraphStorePort.impacted(): everything depending on a node."""

    root: str
    levels: list[list[dict[str, Any]]]
    services: list[str]
    total_impacted: int


class CallerSummary(TypedDict):
    """Endpoints/service-methods/repositories that transitively call into a graph node."""

    endpoints: list[dict[str, Any]]
    service_methods: list[dict[str, Any]]
    repositories: list[dict[str, Any]]


# --------------------------------------------------------------------------- #
# Content / ingestion
# --------------------------------------------------------------------------- #
class ContentType(str, Enum):
    """Kind of source file/content a chunk was extracted from."""

    JAVA = "java"
    SEMANTIC = "semantic"
    MAVEN = "maven"
    GRADLE = "gradle"
    SPRING_YAML = "spring_yaml"
    LIQUIBASE = "liquibase"
    FLYWAY = "flyway"
    OPENAPI = "openapi"
    DOCS = "docs"
    INCIDENTS = "incidents"
    # --- Phase 2: infra-as-code (Dockerfile / Kubernetes / Kustomize) ---
    INFRA = "infra"
    # --- Phase 4: generic multi-language extraction engine ---
    PYTHON = "python"
    GO = "go"
    TYPESCRIPT = "typescript"
    TSX = "tsx"
    JAVASCRIPT = "javascript"
    CSHARP = "csharp"
    RUST = "rust"
    RUBY = "ruby"
    PHP = "php"
    C = "c"
    CPP = "cpp"
    BASH = "bash"


class SourceFile(BaseModel):
    """A single file discovered inside a repository."""

    repo: str
    rel_path: str
    abs_path: str
    content_type: ContentType
    sha256: str
    size_bytes: int


class Chunk(BaseModel):
    """A retrievable unit of text with provenance and embedding metadata."""

    id: str
    repo: str
    rel_path: str
    content_type: ContentType
    collection: str
    text: str
    start_line: int | None = None
    end_line: int | None = None
    # Free-form provenance, e.g. {"symbol": "OrderController", "kind": "class"}
    metadata: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Knowledge graph
# --------------------------------------------------------------------------- #
class NodeType(str, Enum):
    """Every node label used in the microservices knowledge graph."""

    SERVICE = "Service"
    GATEWAY = "Gateway"
    BUSINESS_CAPABILITY = "BusinessCapability"
    EXECUTION_FLOW = "ExecutionFlow"
    BUSINESS_FLOW = "BusinessFlow"
    TECHNICAL_FLOW = "TechnicalFlow"
    CONTROLLER = "Controller"
    ENDPOINT = "Endpoint"
    DTO = "DTO"
    ENTITY = "Entity"
    REPOSITORY = "Repository"
    TABLE = "Table"
    KAFKA_PRODUCER = "KafkaProducer"
    KAFKA_CONSUMER = "KafkaConsumer"
    TOPIC = "Topic"
    QUEUE = "Queue"
    DATABASE = "Database"
    CONFIG = "Config"
    INCIDENT = "Incident"
    SERVICE_LAYER = "ServiceLayer"   # @Service / @Component business logic classes
    METHOD = "Method"                # service/repository method
    DESIGN_PATTERN = "DesignPattern" # detected architectural/design pattern
    DEPENDENCY = "Dependency"        # Maven/Gradle library dependency
    SCHEMA_COLUMN = "SchemaColumn"   # JPA entity column / DB column
    EXCEPTION_TYPE = "ExceptionType" # custom exception class
    TEST_CLASS = "TestClass"         # unit / integration test class
    CONFIG_PROPERTY = "ConfigProperty"  # individual spring property key
    MIGRATION = "Migration"          # Flyway/Liquibase migration script
    # --- Enterprise graph schema additions (additive, non-breaking) ---
    GIT_REPOSITORY = "GitRepository"  # the source repo itself (distinct from JPA Repository)
    MODULE = "Module"                # Maven/Gradle module within a repo
    PACKAGE = "Package"              # Java package
    INTERFACE = "Interface"          # Java interface (distinct from concrete class nodes)
    CLASS = "Class"                  # generic class node (superset of Controller/Entity/etc.)
    FIELD = "Field"                  # class/instance field
    PARAMETER = "Parameter"          # method/constructor formal parameter
    ANNOTATION_USAGE = "AnnotationUsage"  # annotation applied at a code site
    LOGICAL_BLOCK = "LogicalBlock"   # if/for/try/switch/synchronized/lambda block
    CONSTRUCTOR = "Constructor"      # type constructor
    EXCEPTION = "Exception"          # runtime exception occurrence tracked for RCA (vs. ExceptionType = the class)
    # --- Repository Intelligence additions ---
    TECHNOLOGY = "Technology"        # technology detected in a repo's own manifests
    ARCHITECTURE = "Architecture"    # architectural style discovered for a repo
    # --- Dependency Intelligence additions ---
    DEP_ARTIFACT = "DepArtifact"     # canonical package (maven/nuget/pip)
    DEP_FEATURE = "DepFeature"       # capability/feature cluster (e.g. "Web MVC")
    # --- Follow-on gap 7: deeper Spring Boot domain modeling ---
    BEAN_DEFINITION = "BeanDefinition"  # a @Bean-annotated factory method's produced bean
    # --- Phase 2 additions ---
    FILE = "File"                    # a source file (git co-change coupling endpoint)
    INFRA_RESOURCE = "InfraResource"  # Dockerfile/K8s/Kustomize resource declared in a repo


class EdgeType(str, Enum):
    """Every relationship label used in the microservices knowledge graph."""

    EXPOSES = "EXPOSES"
    DECLARED_IN = "DECLARED_IN"
    RETURNS = "RETURNS"
    ACCEPTS = "ACCEPTS"
    MAPS_TO = "MAPS_TO"
    QUERIES = "QUERIES"
    PRODUCES_TO = "PRODUCES_TO"
    CONSUMES_FROM = "CONSUMES_FROM"
    CALLS = "CALLS"
    DEPENDS_ON = "DEPENDS_ON"
    AFFECTS = "AFFECTS"
    DELEGATES_TO = "DELEGATES_TO"    # controller→service, service→repository
    THROWS = "THROWS"                # endpoint/method→exception type
    USES_CONFIG = "USES_CONFIG"      # class→config property (@Value)
    IMPLEMENTED_BY = "IMPLEMENTED_BY"  # interface→implementing class
    USES_PATTERN = "USES_PATTERN"    # class→design pattern node
    HAS_COLUMN = "HAS_COLUMN"        # entity→column
    HAS_RELATIONSHIP = "HAS_RELATIONSHIP"  # entity→entity (JPA)
    TESTED_BY = "TESTED_BY"          # class→test class
    EXTENDS = "EXTENDS"              # exception hierarchy / class inheritance
    HAS_MIGRATION = "HAS_MIGRATION"  # table→migration script
    USES_DEPENDENCY = "USES_DEPENDENCY"  # service→library dependency
    HAS_FIELD = "HAS_FIELD"          # type→field
    HAS_PARAMETER = "HAS_PARAMETER"  # method/constructor→parameter
    HAS_ANNOTATION = "HAS_ANNOTATION"  # owner→annotation usage
    CONTAINS_BLOCK = "CONTAINS_BLOCK"  # method→logical block
    # --- Enterprise graph schema additions (additive, non-breaking) ---
    IMPLEMENTS = "IMPLEMENTS"        # class→interface (alias of IMPLEMENTED_BY, inverse direction)
    CATCHES = "CATCHES"              # method→exception type it catches
    USES = "USES"                    # generic class/method→dependency usage
    READS = "READS"                  # method/repository→table (read access)
    WRITES = "WRITES"                # method/repository→table (write access)
    PUBLISHES = "PUBLISHES"          # producer→topic (alias of PRODUCES_TO, spec-exact name)
    CONSUMES = "CONSUMES"            # consumer→topic (alias of CONSUMES_FROM, spec-exact name)
    BELONGS_TO = "BELONGS_TO"        # node belongs to a parent grouping
    RELATED_TO = "RELATED_TO"        # general semantic relationship
    PART_OF = "PART_OF"              # component of a larger module
    REQUIRES = "REQUIRES"            # node requires another to function
    GENERATES = "GENERATES"          # annotation/config generates a class/behavior at runtime
    # --- Dependency Intelligence additions ---
    PROVIDES_FEATURE = "PROVIDES_FEATURE"        # DEP_ARTIFACT → DEP_FEATURE
    TRANSITIVE_DEP = "TRANSITIVE_DEP"            # DEP_ARTIFACT → DEP_ARTIFACT
    # P3.2: Feign client method → the specific endpoint it invokes on the target service
    RESOLVES_TO = "RESOLVES_TO"
    # --- Phase 2: graph model enrichment (additive, non-breaking) ---
    SIMILAR_TO = "SIMILAR_TO"            # method↔method near-duplicate (MinHash/LSH Jaccard)
    SEMANTICALLY_RELATED = "SEMANTICALLY_RELATED"  # vocabulary-mismatch bridge (embedding cosine)
    DATA_FLOWS = "DATA_FLOWS"            # caller arg → callee parameter dataflow
    FILE_CHANGES_WITH = "FILE_CHANGES_WITH"  # git co-change coupling between two files
    EMITS = "EMITS"                      # producer → event/queue (generic pub-sub, non-Kafka)
    LISTENS_ON = "LISTENS_ON"            # consumer → event/queue (generic pub-sub, non-Kafka)
    GRPC_CALLS = "GRPC_CALLS"            # gRPC client stub → gRPC service method
    GRAPHQL_RESOLVES = "GRAPHQL_RESOLVES"  # @QueryMapping/@MutationMapping method → GraphQL field
    # --- Follow-on: runtime trace overlay (opt-in, never mutates static CALLS) ---
    RUNTIME_CALL = "RUNTIME_CALL"  # observed production call, from ingest_traces


class GraphNode(BaseModel):
    """A single node in the knowledge graph."""

    id: str
    type: NodeType
    name: str
    service: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    """A single directed relationship between two graph nodes."""

    src: str
    dst: str
    type: EdgeType
    attributes: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Dependency Intelligence models
# --------------------------------------------------------------------------- #
class DependencyArtifact(BaseModel):
    """A canonical package artifact discovered across repos."""

    id: str                     # "{ecosystem}:{group}:{artifact}" normalised
    ecosystem: str              # maven | nuget | pip
    group: str                  # groupId / namespace / "" for pip
    artifact: str               # artifactId / PackageId / package name
    version: str = ""           # declared version (may be "managed")
    scope: str = "compile"      # compile | runtime | test | provided
    optional: bool = False
    is_spring_starter: bool = False
    is_internal: bool = False   # True when groupId matches known internal prefixes
    features: list[str] = Field(default_factory=list)     # mapped capability tags
    concept_names: list[str] = Field(default_factory=list)  # API symbols this artifact surfaces
    services: list[str] = Field(default_factory=list)     # repos that declare this dep


# --------------------------------------------------------------------------- #
# Parser output: everything a single file yields
# --------------------------------------------------------------------------- #
class ParseResult(BaseModel):
    """Nodes, edges and chunks extracted from one source file."""

    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    chunks: list[Chunk] = Field(default_factory=list)

    def extend(self, other: ParseResult) -> None:
        """Merge another ParseResult's nodes/edges/chunks into this one in place."""
        self.nodes.extend(other.nodes)
        self.edges.extend(other.edges)
        self.chunks.extend(other.chunks)


# --------------------------------------------------------------------------- #
# Retrieval
# --------------------------------------------------------------------------- #
class RetrievedChunk(BaseModel):
    """A chunk returned by retrieval, carrying its relevance score and source."""

    chunk: Chunk
    score: float
    source: str = "vector"  # vector | graph | fused


class ToolResponse(BaseModel):
    """Uniform envelope returned by every MCP tool (goal #10)."""

    tool: str
    query: dict[str, Any]
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    citations: list[dict[str, Any]] = Field(default_factory=list)
    markdown: str = ""
    generated_at: datetime = Field(default_factory=_now)
    # Staleness transparency: per-source age + oldest-source flag, so a caller
    # can tell "this answer is based on a doc harvested/indexed 40 days ago"
    # instead of assuming everything is current. Empty dict = not computed for
    # this tool yet (see nlp/freshness.py for who currently populates it).
    freshness: dict[str, Any] = Field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return this response as a plain JSON-serializable dict."""
        return self.model_dump(mode="json")


# --------------------------------------------------------------------------- #
# RCA / Incident domain model (first-class entities, not just log blobs)
# --------------------------------------------------------------------------- #
class Severity(str, Enum):
    """Explainable, hybrid-scored incident severity (never LLM-only)."""

    P1 = "P1"  # critical: major customer/revenue impact, multi-service
    P2 = "P2"  # high: significant impact, single/limited service
    P3 = "P3"  # medium: degraded but workaround exists
    P4 = "P4"  # low: cosmetic / no customer impact


class SeverityFactor(BaseModel):
    """One explainable contribution to a severity score (auditability)."""

    name: str
    weight: float
    input_value: Any
    points: float
    rationale: str


class SeverityScore(BaseModel):
    """Full, explainable severity assessment — the hybrid scoring output."""

    severity: Severity
    total_score: float
    max_score: float
    factors: list[SeverityFactor] = Field(default_factory=list)
    confidence: float = 0.0

    def as_markdown(self) -> str:
        """Render the severity score and its contributing factors as a markdown table."""
        lines = [f"**Severity: {self.severity.value}** "
                 f"(score {self.total_score:.1f}/{self.max_score:.1f}, "
                 f"confidence {self.confidence:.0%})", "", "| Factor | Input | Points | Why |",
                 "|---|---|---|---|"]
        for f in self.factors:
            lines.append(f"| {f.name} | {f.input_value} | {f.points:+.1f} | {f.rationale} |")
        return "\n".join(lines)


class StackFrame(BaseModel):
    """A single parsed stack-trace frame."""

    class_name: str
    method_name: str
    file_name: str | None = None
    line_number: int | None = None
    package: str | None = None
    is_project_code: bool = False


class ExceptionEvent(BaseModel):
    """A single production exception occurrence submitted for RCA."""

    id: str
    exception_type: str
    message: str | None = None
    service_name: str | None = None
    version: str | None = None
    environment: str | None = None
    stack_frames: list[StackFrame] = Field(default_factory=list)
    logs: str | None = None
    occurred_at: datetime = Field(default_factory=_now)


class RCAResult(BaseModel):
    """Structured output of exception / defect root-cause analysis."""

    probable_root_cause: str
    confidence: float
    severity: SeverityScore | None = None
    affected_services: list[str] = Field(default_factory=list)
    suggested_fix: str = ""
    related_code_areas: list[dict[str, Any]] = Field(default_factory=list)
    related_incidents: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class IncidentRecord(BaseModel):
    """First-class, persisted incident entity — the unit of RCA knowledge memory."""

    id: str
    title: str
    description: str = ""
    severity: Severity = Severity.P3
    status: str = "open"  # open | investigating | resolved | closed
    service_name: str | None = None
    environment: str | None = None
    version: str | None = None
    exception_type: str | None = None
    exception_class: str | None = None
    exception_method: str | None = None
    stack_trace: str | None = None
    logs: str | None = None
    root_cause: str | None = None
    rca_document: str | None = None
    confidence: float = 0.0
    fix_summary: str | None = None
    resolution: str | None = None
    related_commit: str | None = None
    affected_services: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)
    resolved_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
