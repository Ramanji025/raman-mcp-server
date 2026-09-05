# Knowledge Base Feed Workflow — Overview

End-to-end sequence for how this tool ingests your microservice repositories and
turns them into a queryable knowledge base (Neo4j graph + Qdrant vectors +
Postgres/JSON metadata). Commands assume Windows PowerShell with the venv
activated (`.\.venv\Scripts\Activate.ps1`); Linux/macOS equivalents drop the
`.\` and `.venv\Scripts\` prefixes.

> **Scope note:** this platform indexes **your own code only**. It does not crawl
> or store public technology documentation — the LLM already knows Spring/Java,
> and live web gap-fill covers the long tail on demand.

## 0. Prerequisites (one-time)
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e .
Copy-Item .env.example .env    # then edit LEARNING_ENABLED, LLM_*, etc.
```
Edit [config/repos.yaml](config/repos.yaml) to list the microservice repos to index.

## 1. Start infrastructure
```powershell
docker compose up -d qdrant postgres neo4j
docker compose ps
```
- Qdrant: `http://localhost:6333` (vectors)
- Neo4j: `bolt://localhost:7687`, browser `http://localhost:7474` (graph)
- Postgres: `localhost:5432/mcpkb` (metadata, incidents, audit log)

Optional local LLM (used for enrichment + narrative synthesis):
```powershell
ollama pull gemma4:26b
ollama list
```

## 2. Build the knowledge base
```powershell
mcp-kb-build-all
```
This single command runs the entire pipeline below. Useful flags:

| Flag | Meaning |
|---|---|
| `--repo <name>` | Limit the build to one repository |
| `--skip-clone` | Skip git clone/pull (use the local checkout as-is) |
| `--skip-dependencies` | Skip Stage 5 dependency intelligence |
| `--skip-embeddings` | Skip the vector indexing stage |
| `--skip-llm` / `--llm-workers 0` | Skip Ollama enrichment |
| `--pilot-llm` | Use the smaller pilot enrichment script |
| `--allow-llm-failure` | Don't fail the build on LLM errors |

## 3. What `mcp-kb-build-all` does, stage by stage

1. **Repository Intelligence** — detects language, frameworks, architecture style,
   and repo type, then picks which parsers to run (`RepositoryIntelligenceEngine`).
2. **Project Parser** — clones/pulls each repo in `config/repos.yaml`, scans
   supported source and config files, and parses Java/Spring code with tree-sitter
   into graph nodes and retrievable chunks (`ParserRegistry`).
3. **Project Knowledge Graph** — writes services, controllers, endpoints, entities,
   tables, Kafka producers/consumers, execution flows, and API contracts to Neo4j
   (`KnowledgeBuilder`).
4. **Incident Knowledge Graph** — parses incident markdown into the graph for RCA.
5. **Dependency Intelligence** — scans `pom.xml` / `.csproj` / `requirements.txt`,
   maps artifacts to capability features, resolves transitive deps, writes
   `DepArtifact` / `DepFeature` nodes.
6. **Vector Embeddings** — embeds chunks into Qdrant (`mcpkb_*` collections),
   dense (fastembed) plus BM25 sparse when `SPARSE_ENABLED=true`.
7. **LLM Semantic Enrichment** — Ollama summarizes types/methods/fields onto Neo4j
   nodes (`llm_summary`), then syncs those summaries into `mcpkb_semantic` so
   `ask` can retrieve them.

Staged equivalent (Stages 1–4, 6–7 only):
```powershell
mcp-kb-rewrite
mcp-kb-rewrite --repo dic-store-service
mcp-kb-rewrite --skip-clone --skip-llm-enrichment
```

## 4. Routine operations
```powershell
# Incremental re-ingest (skips unchanged files, no embeddings)
mcp-kb-ingest

# Incremental refresh + backfill embeddings/BM25 on changed files
mcp-kb-refresh
mcp-kb-refresh --repo dic-store-service

# Re-run LLM enrichment only
.\.venv\Scripts\python.exe scripts\llm_enrich_full.py

# Retrieval eval (needs a live KB)
mcp-kb-eval
```
`mcp-kb-build-all` is safe to re-run — it skips unchanged files by hash and only
re-embeds/re-enriches what actually changed.

## 5. Wipe and rebuild from scratch
```powershell
docker compose down -v
Remove-Item -Recurse -Force .\data\graph, .\data\knowledge, .\data\semantic, `
  .\data\learning -ErrorAction SilentlyContinue
Remove-Item -Force .\data\metadata.json, .\data\audit_log.jsonl -ErrorAction SilentlyContinue
docker compose up -d qdrant postgres neo4j
mcp-kb-build-all
```
(`data/repos` is kept so repos aren't re-cloned unless you also delete it.)

Apply the Neo4j schema on a fresh database:
```powershell
docker compose exec neo4j cypher-shell -f /scripts/neo4j_enterprise_schema.cypher
```

## 6. Serve the knowledge base
```powershell
mcp-kb-server
```
Point an MCP client (VS Code, Claude Desktop, Cursor) at this server, then call
the `ask` tool with plain English. Rate answers with `rate_answer` to feed the
online learner (aliases + retrieval boosts persisted under `data/learning/`).

When project evidence is thin, `ask` gap-fills from a live web search
(`WEB_SEARCH_ENABLED`, gated by `WEB_SEARCH_MIN_CONFIDENCE`) rather than from a
pre-crawled documentation corpus.

## Storage summary
| Data | Location |
|---|---|
| Project code + architecture graph | Neo4j |
| Typed knowledge objects | `data/knowledge/objects.jsonl` |
| Semantic summary cache | `data/semantic/summary_cache.json` |
| Vectors (dense + BM25) | Qdrant `mcpkb_*` collections |
| Ingest metadata / incidents / audit log | PostgreSQL (or JSON fallback) |
| Usage/learning memory | `data/learning/state.json`, `events.jsonl` |

## Quick reference: end-to-end command sequence
```powershell
docker compose up -d qdrant postgres neo4j
mcp-kb-build-all
mcp-kb-server
# ...later, after code changes...
mcp-kb-refresh
```
