# Open WebUI MCP Integration

This guide connects Open WebUI to the MCP Microservices Knowledge Base as its
default engineering knowledge server. The server exposes standard streamable
HTTP MCP, so Open WebUI discovers tools, prompts, and resources through the
MCP protocol instead of through a custom OpenAPI adapter.

## 1. Start the MCP Server

### One-command Windows launcher

The repository includes a PowerShell launcher that starts the complete Docker
stack, opens Open WebUI, and prints the in-container MCP endpoint to register:

```powershell
Set-Location C:\Users\pathram01\Documents\Github\mcp-microservices-kb
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\scripts\start-openwebui-stack.ps1
```

The launcher prompts for the Neo4j password without saving it to source control.
It exposes this stack's Open WebUI on `http://localhost:3001` by default, so it
does not collide with an existing Open WebUI instance on port `3000`. Override
the host port when needed:

```powershell
.\scripts\start-openwebui-stack.ps1 -OpenWebUiPort 3002
```
To rebuild the indexed knowledge after the containers start, use:

```powershell
.\scripts\start-openwebui-stack.ps1 -BuildKnowledge
```

Use `-SkipOllamaEnrichment` when Ollama is not running or when only static
graph/vector knowledge is required:

```powershell
.\scripts\start-openwebui-stack.ps1 -BuildKnowledge -SkipOllamaEnrichment
```

Open WebUI stores MCP connections in its authenticated application database.
The script cannot safely create that connection without an Open WebUI admin
account/API credential, so the first registration remains a short one-time UI
step described in section 3.

### One-command macOS and Linux launcher

Use the Bash launcher on macOS or Linux. Docker mode is the complete portable
option: it starts Neo4j, Qdrant, PostgreSQL, the MCP server, and Open WebUI.

```bash
cd /path/to/mcp-microservices-kb
chmod +x scripts/start-openwebui-stack.sh
./scripts/start-openwebui-stack.sh
```

Run a full indexing and enrichment build after startup:

```bash
./scripts/start-openwebui-stack.sh --build-knowledge
```

Use `--native` when Neo4j, Qdrant, and Open WebUI are already installed and
running on the host. Native mode creates the project Python virtual environment
when needed, validates Neo4j/Qdrant, and starts the MCP HTTP server; it does not
attempt system-wide database or Open WebUI installation.

```bash
./scripts/start-openwebui-stack.sh --native --open-webui-url http://localhost:3000
```

Use `./scripts/start-openwebui-stack.sh --help` for ports, Python interpreter,
Ollama-enrichment, and troubleshooting options.

### Host process on Windows

Start Neo4j, Qdrant, and PostgreSQL first. Then run the MCP server with HTTP
transport:

```powershell
Set-Location C:\Users\pathram01\Documents\Github\mcp-microservices-kb

$env:MCP_KB_TRANSPORT = "http"
$env:MCP_KB_HTTP_HOST = "0.0.0.0"
$env:MCP_KB_HTTP_PORT = "8000"
$env:GRAPH_BACKEND = "neo4j"
$env:NEO4J_URI = "bolt://localhost:7687"
$env:NEO4J_USER = "neo4j"
$env:NEO4J_PASSWORD = "<your-neo4j-password>"
$env:NEO4J_DATABASE = "neo4j"
$env:LLM_PROVIDER = "ollama"
$env:LLM_MODEL = "qwen2.5-coder:14b"
$env:OLLAMA_BASE_URL = "http://localhost:11434/v1"

.\.venv\Scripts\python.exe -m mcp_kb.server
```

FastMCP streamable HTTP uses this MCP endpoint:

```text
http://localhost:8000/mcp
```

### Entire platform in Docker Compose

The repository Compose configuration starts Neo4j, Qdrant, PostgreSQL, and the
MCP HTTP server together.

```powershell
$env:NEO4J_PASSWORD = "<your-neo4j-password>"
docker compose up -d
```

The host MCP endpoint is again:

```text
http://localhost:8000/mcp
```

## 2. Choose the Correct MCP URL

| Open WebUI location | MCP URL |
|---|---|
| Open WebUI runs directly on the Windows host | `http://localhost:8000/mcp` |
| Open WebUI runs in Docker Desktop, MCP server runs on Windows host | `http://host.docker.internal:8000/mcp` |
| Open WebUI and MCP server run in the same Docker Compose network | `http://mcp-server:8000/mcp` |

Use `host.docker.internal` only from a Docker container on Docker Desktop. Do
not use `localhost` inside an Open WebUI container: it refers to the Open WebUI
container itself, not the Windows host.

## 3. Add the Server in Open WebUI

In Open WebUI:

1. Open **Admin Panel**.
2. Open **Settings** or **External Connections**.
3. Open the **MCP Servers** section.
4. Add a new server named `microservices-knowledge-base`.
5. Select **Streamable HTTP** as the transport type.
6. Enter the URL from the table above.
7. Enable the connection and save it.
8. Refresh the tool list or restart Open WebUI if the UI does not refresh it automatically.

The MCP initialization sequence discovers:

- 43+ tools
- reusable MCP prompts
- addressable resources
- server instructions describing the graph-backed engineering assistant

No manual Open WebUI function definitions are required.

## 4. Make It the Default Engineering Server

Enable the MCP server for the target Open WebUI model or workspace. Then add the
following text as the model/workspace system prompt. This is a fallback for
Open WebUI versions that do not display MCP prompts as a separate prompt menu.

```text
You are the default engineering assistant for a Spring Boot microservice platform.
Use the microservices-knowledge-base MCP tools before making claims about code,
architecture, execution flows, incidents, service ownership, or dependencies.
Prefer graph-backed and cited evidence. Use search_code for implementation,
explain_service or explain_architecture for system understanding,
get_execution_flow or trace_execution_path for flows, impact_analysis or
cross_service_impact for change risk, and analyse_exception plus
find_related_incidents for production incidents. State uncertainty when evidence
is incomplete; do not invent service names, APIs, database tables, Kafka topics,
or root causes.
```

The server exposes this same policy as the MCP prompt:

```text
default_platform_assistant
```

## 5. Configured MCP Prompts

Open WebUI clients that support MCP prompt discovery can invoke these directly.
Otherwise, paste the corresponding request into chat; the default system prompt
will route to the same tools.

| Prompt | Input | Purpose |
|---|---|---|
| `default_platform_assistant` | none | Default evidence-first routing policy |
| `investigate_defect` | `problem_statement` | RCA and remediation workflow |
| `plan_feature` | `requirement` | Cross-service feature planning |
| `onboard_new_developer` | `service_name` | Service knowledge transfer workflow |
| `document_api` | `endpoint_path` | API contract and call-chain documentation |
| `review_branch_changes` | `repo_name`, `branch1`, `branch2` | Branch diff and risk review |
| `architect_review` | `service_name` | Security, dependency, schema, pattern, and test review |
| `generate_api_docs` | `service_name` | OpenAPI and developer documentation generation |
| `fix_method_logic` | `class_name`, `method_name`, `issue_description` | Targeted method investigation and fix plan |
| `implement_user_story` | `story_id_or_description` | Story-to-code implementation analysis |
| `sprint_planning` | `sprint_description` | Cross-service sprint impact planning |
| `explain_this_code` | `file_path`, `start_line`, `end_line` | Evidence-backed code explanation |
| `code_review` | `file_path` | Architecture/security/test-focused review |
| `review_defect` | `ticket_id` | Defect diff review with unmodified diff blocks |

## 6. Key Tool Routing

| User request | Primary MCP tool |
|---|---|
| Find implementation | `search_code` or `find_similar_code` |
| Explain a service | `explain_service` or `full_kt` |
| Find an API | `find_endpoint` or `get_api_contract` |
| Trace execution | `get_execution_flow`, `trace_execution_path`, or `trace_call_chain` |
| Method callers/callees | `find_callers` or `find_callees` |
| Change impact | `impact_analysis` or `cross_service_impact` |
| Service dependencies | `dependency_analysis`, `service_dependents`, or `find_api_path` |
| Kafka topology | `kafka_topology` |
| Incident/RCA | `analyse_exception`, `analyze_defect`, or `find_related_incidents` |
| Business mapping | `explain_business_capability` |
| Version comparison | `compare_versions` or `compare_branches` |

## 7. Verify the Connection

After Open WebUI connects, ask:

```text
Give me the whole-system architecture summary and identify the highest-degree services.
```

The assistant should call `generate_architecture_summary` or
`explain_architecture` and return evidence from Neo4j/Qdrant.

For a narrower test, ask:

```text
Explain service dic-store-service, including APIs, tables, Kafka events, and dependencies.
```

If tool calls do not appear, confirm the Open WebUI MCP server URL ends in
`/mcp`, verify port `8000` is reachable from the Open WebUI host/container, and
reconnect the server in the Open WebUI administration page.

## 8. Security and Operations

The local MCP HTTP endpoint has no built-in authentication layer. Keep it bound
to a trusted local network, do not publish port `8000` to the public Internet,
and place it behind an authenticated reverse proxy before multi-user or remote
access.

For Open WebUI in Docker Desktop, the MCP server can remain on the Windows host
and use `host.docker.internal`. For enterprise deployment, run both services in
a private Docker/Kubernetes network and expose only the Open WebUI frontend.
