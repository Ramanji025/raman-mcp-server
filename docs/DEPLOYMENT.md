# Local deployment guide

## Prerequisites

- **Python 3.12**
- **Docker Desktop** (for Qdrant and, optionally, Postgres)
- **Git** on `PATH`
- ~4 GB free RAM (embeddings + Qdrant + graph)

## 1. Start infrastructure

```powershell
cd mcp-microservices-kb
docker compose up -d qdrant postgres
```

Qdrant dashboard: <http://localhost:6333/dashboard>. To run with **zero external
services**, set `POSTGRES_ENABLED=false` (metadata falls back to a local JSON file);
Qdrant is still required for vector search.

## 2. Install the package

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e .
```

The first run downloads the local embedding model (`BAAI/bge-small-en-v1.5`).

## 3. Configure

```powershell
copy .env.example .env
```

Key settings in `.env`:

| Variable | Meaning |
|----------|---------|
| `MCP_KB_REPOS_ROOT` | Folder holding the cloned service repos |
| `MCP_KB_REPOS_MANIFEST` | `config/repos.yaml` with clone URLs |
| `QDRANT_URL` | Qdrant endpoint (default `http://localhost:6333`) |
| `EMBEDDING_PROVIDER` | `fastembed` (offline) · `sentence-transformers` · `openai` |
| `LLM_PROVIDER` / `OPENAI_API_KEY` | Optional; enables narrative synthesis |
| `POSTGRES_ENABLED` | `true` to use Postgres metadata store |

Edit `config/repos.yaml` with your real repo URLs (or skip cloning and point
`MCP_KB_REPOS_ROOT` at existing checkouts).

## 4. Ingest

```powershell
mcp-kb-ingest --clone     # clone/pull all 19 repos from the manifest
mcp-kb-ingest             # full ingest -> Qdrant + graph.json + metadata

# Single repo:
mcp-kb-ingest --repo rx-order-service
```

You'll see a Rich table summarizing files, chunks, nodes and edges per repo.

## 5. Run the server

### stdio (for IDE / agent clients)

```powershell
mcp-kb-server
```

### HTTP

```powershell
$env:MCP_KB_TRANSPORT="http"; mcp-kb-server
# serves on http://127.0.0.1:8000
```

### Full stack via Docker

```powershell
docker compose up -d      # qdrant + postgres + mcp-server (HTTP on :8000)
```

## 6. Wire up an MCP client

A ready-to-paste registration lives in [config/mcp-client.json](../config/mcp-client.json).

**Claude Desktop / VS Code MCP (stdio)** — add to your MCP config:

```json
{
  "mcpServers": {
    "microservices-kb": {
      "command": "mcp-kb-server",
      "env": {
        "MCP_KB_REPOS_ROOT": "C:/work/rx/repos",
        "QDRANT_URL": "http://localhost:6333"
      }
    }
  }
}
```

**HTTP clients** connect to `http://127.0.0.1:8000/mcp`.

Beyond the nine tools, the server also exposes MCP **resources**
(`kb://architecture/summary`, `kb://services`, `kb://service/{name}`) and
**prompts** (`investigate_defect`, `plan_feature`) that compatible clients can
attach or invoke directly.

## 7. Keep it fresh

```powershell
mcp-kb-refresh            # git pull + incremental re-index (changed files only)
```

Schedule it (Windows Task Scheduler / cron) for continuous freshness.

## 8. Verify

```powershell
python scripts/smoke_test.py         # lists registered tools/resources/prompts
pytest -m "not slow"                 # offline unit tests
pytest tests/integration             # graph tests offline; vector test needs Qdrant
```

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `graph_snapshot_missing` on server start | Run `mcp-kb-ingest` first |
| Qdrant connection refused | `docker compose up -d qdrant`; check `QDRANT_URL` |
| Postgres errors | Set `POSTGRES_ENABLED=false` to use the JSON fallback |
| Slow first request | Embedding model is loading/warming; subsequent calls are fast |
| Empty tool results | Confirm repos were ingested and `MCP_KB_REPOS_ROOT` is correct |
| stdio client sees garbage | Logs go to **stderr**; never print to stdout in tools |
| `pip` fails: `CERTIFICATE_VERIFY_FAILED ... self-signed certificate` | Corporate SSL interception — see below |

### Installing behind a corporate SSL proxy

Corporate networks often terminate TLS with a self-signed root CA, which breaks
`pip`'s certificate verification. Two options:

**Preferred (secure):** point pip at your corporate root CA bundle.

```powershell
pip install --cert C:/path/to/corporate-root-ca.pem -r requirements.txt
# or persist it:  setx PIP_CERT C:\path\to\corporate-root-ca.pem
```

**Fallback (skips verification for PyPI only):**

```powershell
pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org -r requirements.txt
```

Running tests offline (no Qdrant/Postgres) needs only a subset:

```powershell
pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org `
    pydantic pydantic-settings pyyaml structlog networkx tree-sitter `
    tree-sitter-java lxml pytest pytest-asyncio qdrant-client fastmcp langgraph langchain-core
pytest        # 16 passed, 1 skipped (the Qdrant vector test)
```
