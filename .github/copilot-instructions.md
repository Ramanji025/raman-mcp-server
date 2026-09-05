# GitHub Copilot Instructions — mcp-microservices-kb

## Default Skill (mandatory for every code change)
This repo is Python-only. Before writing or reviewing any code, apply the **python-pro** skill
defined in [SKILL.md](../SKILL.md) (Python 3.12+, uv/ruff/pydantic/FastAPI conventions, type hints,
async patterns, testing with pytest). Treat it as always-active, not opt-in:
- Confirm runtime/dependencies/perf targets before implementing.
- Use modern typing (PEP 604 `X | None`, generics, Protocols) on all new/edited code.
- Prefer `ruff` formatting conventions and PEP 8.
- Add/keep docstrings on public functions/classes; keep comments minimal.
- Use specific exceptions, never bare `except:`.
- Add or update pytest tests for changed behavior when feasible.

## Project Context
This is a **Spring Boot microservices knowledge base** MCP server. It ingests Java/Spring Boot
source repositories and serves structured knowledge about APIs, service layers, entities,
design patterns, Kafka events, and cross-service dependencies.

## Before Writing Code

**Always query the MCP tools first to understand existing patterns and contracts:**

```
# Find existing API contracts before adding new endpoints
get_api_contract("<path or endpoint name>")

# Understand a service fully before modifying it
full_kt("<service-name>")

# Check blast radius before changing an entity or table
impact_analysis("<entity-or-table-name>")

# Find existing endpoints before creating duplicates
find_endpoint("<operation description>")
```

## Spring Boot Conventions (this codebase)

### Package Structure
```
com.example.<service>/
  controller/     → @RestController classes (@RequestMapping, @GetMapping, etc.)
  service/        → @Service interfaces + implementations
  repository/     → @Repository extending JpaRepository / CrudRepository
  entity/         → @Entity JPA classes
  dto/            → Request/Response POJOs or Java Records
  config/         → @Configuration classes
  event/          → Kafka producers (@KafkaTemplate) and consumers (@KafkaListener)
  exception/      → Custom exceptions + @ControllerAdvice handlers
```

### Controller Conventions
- Always use `ResponseEntity<T>` as return type for REST methods.
- Use constructor injection (not @Autowired field injection) for dependencies.
- Validate input DTOs with `@Valid` and add JSR-303 annotations on fields.
- Use `@PreAuthorize` for method-level security.

### Service Layer Conventions
- Service interfaces live in `service/`, implementations in `service/impl/`.
- Mark write operations `@Transactional`, read-only queries `@Transactional(readOnly = true)`.
- Throw specific checked/unchecked exceptions (e.g. `ResourceNotFoundException`).
- Use `@Value("${property.key}")` for configuration — register keys in `application.yml`.

### Repository Conventions
- Prefer derived query methods (`findByXxx`) for simple lookups.
- Use `@Query` (JPQL or native) for complex queries.
- Return `Optional<T>` for single-entity lookups.

### DTO Conventions
- Use Java Records for immutable DTOs where possible.
- Annotate validation: `@NotNull`, `@NotBlank`, `@Size`, `@Email`, etc.
- Separate Request and Response DTOs — never expose JPA entities directly.

### Error Handling
- `NotFoundException` / `ResourceNotFoundException` → 404
- `BadRequestException` / `ValidationException` → 400
- `UnauthorizedException` → 401
- `ForbiddenException` → 403
- Always return a structured error body `{ "status": 4xx, "message": "...", "timestamp": "..." }`.

## Design Patterns in Use
The knowledge graph detects the following patterns automatically — follow them consistently:
- **Repository Pattern** — `@Repository` classes
- **Proxy / Feign Client** — `@FeignClient` for inter-service calls
- **Cache-Aside** — `@Cacheable` / `@CacheEvict`
- **Circuit Breaker** — `@CircuitBreaker` (Resilience4j)
- **Retry Pattern** — `@Retry` (Resilience4j)
- **Observer / Event** — `@KafkaListener`, `@EventListener`
- **AOP / Decorator** — `@Aspect`
- **Async Pattern** — `@Async`
- **Transactional / Unit of Work** — `@Transactional`

## MCP Tool Reference

| Tool | Purpose |
|---|---|
| `find_endpoint` | Find existing API endpoint by description/path |
| `explain_service` | Detailed breakdown of one microservice |
| `get_api_contract` | Full contract: auth, request/response JSON, errors, call chain |
| `full_kt` | Complete KT: all APIs, patterns, flows, config, events |
| `impact_analysis` | Blast radius for changing an entity/table/service |
| `compare_branches` | API change diff between git branches |
| `trace_business_flow` | End-to-end flow across services |
| `analyze_defect` | Root-cause a bug |
| `implement_feature` | Implementation plan for a new requirement |
| `search_domain_knowledge` | Semantic search across code + docs |
| `generate_architecture_summary` | Full ecosystem overview with Mermaid diagram |
| `onboarding_assistant` | Reading list + learning path for a topic |

## Ingestion

To re-index after adding/modifying source repos in `data/repos/`:
```powershell
cd C:\Users\pathram01\Documents\Github\mcp-microservices-kb
.\.venv\Scripts\python.exe -m mcp_kb.ingestion.pipeline
```

## Infrastructure
- **Qdrant** vector DB: `http://localhost:6333` (start: `.\qdrant-bin\qdrant.exe`)
- **PostgreSQL**: `postgresql://mcpkb:mcpkb@localhost:5432/mcpkb`
- **Python venv**: `.\.venv\Scripts\python.exe`
- **MCP transport**: stdio via `cmd.exe /c python.exe -m mcp_kb.server`
