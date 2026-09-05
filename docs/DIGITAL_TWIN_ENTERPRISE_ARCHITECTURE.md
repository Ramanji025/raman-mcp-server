# Digital Twin Enterprise Architecture (Final)

Date: 2026-08-18

## 1. Platform Review Summary

Current platform strengths (validated from code + workspace state):
- Ingestion pipeline already supports full + incremental indexing with graph updates and optional embeddings.
- Knowledge graph already captures service, endpoint, method, repository, table, topic, flow, capability, and incident-linked entities.
- Retrieval already uses hybrid fusion (vector + graph), reranking support, and semantic-summary-first collection ordering.
- Incident intelligence already persists structured incidents and links them to graph and vectors.
- Multi-stage workflow already enforces: intent detection -> agent selection -> graph retrieval -> vector retrieval -> reranking -> verification -> final answer.

Observed local scale snapshot:
- Repositories discovered under data/repos: 44
- MCP tools exported from server: 43
- Graph snapshot size: 18,648,226 bytes
- Graph entities from snapshot: 11,142 nodes, 19,909 edges

## 2. Digital Twin Definition for This Platform

Digital Twin = continuously updated, queryable, evidence-grounded model of the engineering system where each technical and operational artifact is represented as a connected graph object.

Twin objects:
- Microservices and libraries
- APIs and method call chains
- Databases/tables and read-write paths
- Kafka topics and producer-consumer paths
- Build/runtime versions and branch/tag lineage
- Incidents, stack traces, RCA hypotheses, fix outcomes
- Business capabilities mapped to technical execution
- Ownership and team boundaries

Twin guarantees:
- Every answer is evidence-backed by graph nodes/edges and/or retrieved chunks.
- Every incremental code change updates affected twin objects.
- Every incident enriches the twin for future RCA and similarity matching.

## 3. Final Enterprise Architecture

### 3.1 Layered Architecture

Layer A: Source and Runtime Signals
- Git repositories, branches, tags, commits, PR metadata
- Source code, configs, OpenAPI, schema migrations
- Runtime incident inputs: exceptions, logs, impact signals

Layer B: Ingestion and Enrichment
- Incremental diff detection per repo
- Parsing + enterprise dependency augmentation
- Semantic summary generation with fingerprint cache
- Knowledge object materialization (typed Pydantic models)
- Optional embedding generation (semantic + code + docs + incidents)

Layer C: Digital Twin Persistence
- Graph store
  - Default: local NetworkX snapshot for local-first operation
  - Enterprise mode: Neo4j backend for 100+ services and multi-user traversal
- Vector store (Qdrant)
  - Collections: semantic, code, docs, architecture, defects, incidents
- Metadata and incident stores (Postgres with local fallback)

Layer D: Retrieval and Reasoning
- Hybrid retrieval (semantic-first -> code/docs/architecture -> incidents)
- RRF fusion + optional reranker
- Graph expansion and impact traversal
- Multi-stage verification before final answer

Layer E: MCP Tooling and Consumers
- MCP tools for code search, architecture search, dependency and impact analysis
- Execution flow and business capability exploration
- Incident intelligence and RCA
- Version comparison and documentation generation
- IDE/agent clients over stdio/HTTP

### 3.2 Digital Twin Core Graph Model

Primary node categories:
- Platform: GitRepository, Service, Module, Package, Library
- API: Controller, Endpoint, DTO, Method, Interface, Class
- Data: Entity, Repository, Table, SchemaColumn, Migration
- Event: KafkaProducer, Topic, KafkaConsumer
- Flow: ExecutionFlow, BusinessFlow, TechnicalFlow
- Ops: Incident, ExceptionType, Version, Release
- Business: BusinessCapability, OwnerTeam

Primary edge categories:
- Structural: CONTAINS, IMPLEMENTS, EXTENDS, USES_DEPENDENCY
- Runtime path: EXPOSES, DELEGATES_TO, CALLS, THROWS, CATCHES
- Data path: QUERIES, READS, WRITES, MAPS_TO
- Event path: PRODUCES_TO, CONSUMES_FROM, PUBLISHES, CONSUMES
- Risk path: AFFECTS, DEPENDS_ON
- Governance: OWNED_BY, SUPPORTS_CAPABILITY
- Incident path: OCCURRED_IN, IMPACTS, FIXED_BY, SIMILAR_TO

## 4. Capability Coverage (Required)

1. Code Search
- Semantic-first retrieval over class/method/API/repository summaries.
- Fallback to code snippets with citations.

2. Architecture Search
- Graph-first exploration of service boundaries, dependencies, and topology.

3. Dependency Analysis
- Service->service, module->module, and library dependency graph with transitive traversal.

4. Impact Analysis
- Reverse reachability over entities, tables, methods, endpoints, and services.

5. Execution Flow Discovery
- Endpoint-specific flow graph with per-edge sequence numbering across method chains.

6. Incident Intelligence
- Structured incident store + graph/vector linking + historical retrieval.

7. RCA
- Deterministic parsing + severity scoring + similarity-assisted probable root cause and fix recommendations.

8. Version Comparison
- Branch/tag/commit aware comparison with API and risk diffs.

9. Technical Documentation Generation
- Service KT, architecture summaries, OpenAPI generation, and topology reports.

10. Service Ownership Discovery
- Add OwnerTeam nodes and OWNED_BY edges sourced from CODEOWNERS, repo config, and incident assignee metadata.

11. Business Capability Mapping
- Capability nodes linked to APIs, services, entities, and events (supports query by business intent).

## 5. Local LLM Optimization (Ollama-first)

Model roles:
- Generation model (local): qwen2.5-coder:14b or qwen3-coder class model via Ollama
- Optional higher-accuracy model (selective): deepseek-coder-v3 (only for low-confidence verification retries)
- Embedding model: local fastembed-compatible model with stable dimensionality

Token minimization strategy:
- Semantic-summary-first retrieval (already implemented)
- Strict top-k caps per stage
- Collection pre-filtering by intent and service
- Compact context blocks (dedupe by chunk id, trim boilerplate)
- Structured Markdown output pass-through from MCP tools (avoid reformatting churn)
- Cache LLM summaries by fingerprint and avoid re-generation on unchanged symbols

Accuracy strategy:
- Keep verification stage mandatory
- Require grounded-term ratio threshold before confident answer
- If below threshold: auto-switch to graph-only extractive answer + confidence warning
- Use reranker when enabled for ambiguous queries
- Persist feedback loop from resolved incidents to raise future RCA confidence

## 6. Scale Design for 100+ Microservices

Storage and compute:
- Move graph backend to Neo4j for multi-user and large traversal workloads
- Keep local NetworkX as developer fallback and offline mode
- Partition ingestion by repository and run parallel workers
- Use incremental index updates only (never full re-index unless forced)
- Shard Qdrant collections by environment or domain if payload growth requires it

Operational guardrails:
- Enable observability metrics for ingest latency, retrieval latency, grounded ratio, and tool success rate
- Enforce RBAC + audit logging for enterprise usage
- Add daily graph/vector consistency checks

Reliability patterns:
- Idempotent ingestion commits
- Checkpointed enrichment jobs
- Backfill/replay job for missed repo updates
- Snapshot versioning for graph and incident stores

## 7. Update Strategy (Digital Twin Freshness)

On code change:
- Detect changed and deleted paths by git diff
- Remove stale vector chunks and graph nodes by path
- Reparse and rebuild only affected subgraph
- Recompute semantic summaries for changed symbols only (fingerprint cache)

On incident ingestion:
- Persist incident record
- Link to impacted services/endpoints/methods/entities
- Mirror to vector store for similarity search
- Attach suggested fixes and resolution outcomes as feedback edges

On release/tag events:
- Record version nodes and compare snapshots
- Update dependency and API-diff edges

## 8. Implementation Delta to Reach Full Twin Maturity

High priority:
- Add OwnerTeam ingestion (CODEOWNERS + metadata parsers)
- Add explicit Version and Release nodes + compare-time edge materialization
- Add graph integrity validator job (dangling edge detection, duplicate node collapse)
- Add confidence policy engine for auto-fallback behavior

Medium priority:
- Domain partitioning of graph namespaces for very large organizations
- Automatic business capability drift detection across releases
- Incident timeline analytics and MTTR trend edges

## 9. Enterprise Target State (Final)

The final architecture is a dual-store Digital Twin platform:
- Graph is the system-of-relationships and deterministic traversal engine.
- Vector is the semantic evidence engine.
- Multi-stage workflow is the reasoning control plane.
- Incident/RCA subsystem is the operational memory.
- Ollama is the local generation runtime with strict token budgets and verification gates.

This architecture is suitable for 100+ microservices, supports all requested capabilities, and remains local-first while allowing enterprise-scale backend upgrades.
