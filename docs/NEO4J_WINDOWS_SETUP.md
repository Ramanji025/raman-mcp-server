# Neo4j Setup for Windows Docker Desktop

This guide configures Neo4j as the enterprise graph backend for the MCP
Microservices Knowledge Base. It enables the Browser UI, Bolt protocol, APOC,
and persistent storage.

For the node/relationship model, Cypher examples, and index strategy, see
[NEO4J_GRAPH_SCHEMA.md](NEO4J_GRAPH_SCHEMA.md).

## Before You Start

Install and start Docker Desktop for Windows. Confirm Linux containers are
enabled, then run:

```powershell
docker version
docker compose version
```

Choose a strong Neo4j password. Do not use the example password in a shared or
production environment.

```powershell
$env:NEO4J_PASSWORD = "replace-with-a-strong-password"
```

## Local Passwordless Development

For a local-only Docker Desktop setup, this repository defaults to
`NEO4J_AUTH=none` and configures MCP clients with `NEO4J_AUTH_ENABLED=false`.
This removes the password requirement that can otherwise prevent a VS Code
stdio MCP process from connecting to Neo4j.

Apply the change to an existing container without deleting its data volume:

```powershell
docker compose up -d --force-recreate neo4j
```

Then configure host-launched MCP clients with:

```text
GRAPH_BACKEND=neo4j
NEO4J_URI=bolt://localhost:7687
NEO4J_AUTH_ENABLED=false
```

Only use passwordless mode on a trusted local machine. Do not publish port
`7687` or `7474` to an untrusted network while authentication is disabled.
For shared, remote, or production environments, set
`NEO4J_AUTH=neo4j/<strong-password>` and `NEO4J_AUTH_ENABLED=true`, then set
`NEO4J_USER` and `NEO4J_PASSWORD` for every MCP client/server process.

`neo4j:latest` is used below to track the current stable official Docker image.
For a controlled environment, inspect the running version and replace `latest`
with that verified immutable major/minor tag in `docker-compose.yml`.

## 1. Docker Run Command

Run this command from PowerShell for a standalone local Neo4j instance:

```powershell
docker run --name mcpkb-neo4j --detach --restart unless-stopped `
  --publish 7474:7474 --publish 7687:7687 `
  --env NEO4J_AUTH="neo4j/$env:NEO4J_PASSWORD" `
  --env NEO4J_PLUGINS='["apoc"]' `
  --env NEO4J_apoc_export_file_enabled=true `
  --env NEO4J_apoc_import_file_enabled=true `
  --env NEO4J_dbms_security_procedures_unrestricted='apoc.*' `
  --volume mcpkb_neo4j_data:/data `
  --volume mcpkb_neo4j_logs:/logs `
  --volume mcpkb_neo4j_import:/import `
  --volume mcpkb_neo4j_plugins:/plugins `
  --volume "${PWD}\backups\neo4j:/import/backups" `
  neo4j:latest
```

Use either this `docker run` method or the Compose method below, not both with
the same container name and ports.

## 2. Docker Compose Setup

The repository Compose file includes an opt-in `neo4j` service. It uses:

- Browser UI over HTTP: port `7474`
- Bolt protocol for the MCP platform: port `7687`
- APOC downloaded into a persistent plugins volume
- Persistent data, log, import, plugins, and host-accessible backup volumes

Start only Neo4j:

```powershell
$env:NEO4J_PASSWORD = "replace-with-a-strong-password"
docker compose --profile neo4j up -d neo4j
docker compose ps neo4j
```

Start Neo4j with the rest of the local platform:

```powershell
$env:NEO4J_PASSWORD = "replace-with-a-strong-password"
docker compose --profile neo4j up -d
```

The Compose service is equivalent to this configuration:

```yaml
neo4j:
  image: neo4j:latest
  container_name: mcpkb-neo4j
  restart: unless-stopped
  environment:
    NEO4J_AUTH: neo4j/${NEO4J_PASSWORD}
    NEO4J_PLUGINS: '["apoc"]'
    NEO4J_apoc_export_file_enabled: "true"
    NEO4J_apoc_import_file_enabled: "true"
    NEO4J_dbms_security_procedures_unrestricted: "apoc.*"
  ports:
    - "7474:7474"
    - "7687:7687"
  volumes:
    - neo4j_data:/data
    - neo4j_logs:/logs
    - neo4j_import:/import
    - neo4j_plugins:/plugins
    - ./backups/neo4j:/import/backups
```

## 3. MCP Platform Environment Variables

Set these variables in the PowerShell session that runs ingestion or the MCP
server. Add the same values to your MCP client configuration if it launches the
server itself.

```powershell
$env:GRAPH_BACKEND = "neo4j"
$env:NEO4J_URI = "bolt://localhost:7687"
$env:NEO4J_USER = "neo4j"
$env:NEO4J_PASSWORD = "replace-with-a-strong-password"
$env:NEO4J_DATABASE = "neo4j"
```

When the MCP server runs inside the `mcp-server` Compose container, use the
Docker service hostname instead:

```powershell
# Values for an mcp-server container, not for a host PowerShell process.
GRAPH_BACKEND=neo4j
NEO4J_URI=bolt://neo4j:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=<your-password>
NEO4J_DATABASE=neo4j
```

Install the optional Python driver once:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[neo4j]"
```

## 4. Verification

### Verify container and ports

```powershell
docker compose ps neo4j
Test-NetConnection localhost -Port 7474
Test-NetConnection localhost -Port 7687
```

Open the Browser UI at `http://localhost:7474`, then sign in with user `neo4j`
and the password supplied through `NEO4J_PASSWORD`.

### Verify APOC in Browser

Run this Cypher query in Neo4j Browser:

```cypher
RETURN apoc.version() AS apocVersion;
```

### Verify from PowerShell

```powershell
docker exec mcpkb-neo4j cypher-shell -u neo4j -p $env:NEO4J_PASSWORD `
  "RETURN 1 AS connected, apoc.version() AS apocVersion;"
```

### Verify the MCP graph backend

```powershell
$env:GRAPH_BACKEND = "neo4j"
$env:NEO4J_URI = "bolt://localhost:7687"
$env:NEO4J_USER = "neo4j"
$env:NEO4J_PASSWORD = "replace-with-a-strong-password"
.\.venv\Scripts\python.exe -c "from mcp_kb.graph.factory import get_graph_store; from mcp_kb.config import get_settings; graph = get_graph_store(get_settings()); print(graph.stats())"
```

Migrate the existing `graph.json` snapshot after connectivity is verified:

```powershell
.\.venv\Scripts\python.exe scripts\migrate_graph_to_neo4j.py
```

Use `GRAPH_BACKEND=neo4j` during future ingestion runs so new knowledge writes
directly to Neo4j.

## 5. Backup Strategy

Use two layers of backup:

1. Keep the Docker named volume as the live durable store.
2. Export regular logical Cypher backups to `./backups/neo4j`, mounted under
  Neo4j's permitted `/import/backups` directory and visible
   from Windows and should be copied to approved backup storage.

Create a logical APOC backup:

```powershell
New-Item -ItemType Directory -Force .\backups\neo4j | Out-Null
docker exec mcpkb-neo4j cypher-shell -u neo4j -p $env:NEO4J_PASSWORD `
  "CALL apoc.export.cypher.all('/import/backups/mcpkb-backup.cypher', {format: 'cypher-shell'}) YIELD file RETURN file;"
```

For a portable Windows-host timestamp, use a fixed filename:

```powershell
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
docker exec mcpkb-neo4j cypher-shell -u neo4j -p $env:NEO4J_PASSWORD `
  "CALL apoc.export.cypher.all('/import/backups/mcpkb-$stamp.cypher', {format: 'cypher-shell'}) YIELD file RETURN file;"
```

Restore a logical backup into an empty database:

```powershell
docker exec -i mcpkb-neo4j cypher-shell -u neo4j -p $env:NEO4J_PASSWORD `
  < ".\backups\neo4j\mcpkb-YYYYMMDD-HHMMSS.cypher"
```

Test backup restore procedures regularly in a separate disposable Neo4j
container. Do not test restore by overwriting the only working graph.

## 6. Data Persistence Strategy

Docker named volumes survive container restart, recreation, and image updates:

| Volume | Contents | Retention |
|---|---|---|
| `neo4j_data` | Graph database files and indexes | Required durable data |
| `neo4j_logs` | Database logs | Retain for troubleshooting and audit needs |
| `neo4j_import` | Files available to Neo4j imports | Keep only approved import inputs |
| `neo4j_plugins` | Downloaded APOC plugin | Persistent for fast restarts |

The host folder `./backups/neo4j` contains logical exports. Keep it out of Git
and back it up outside the workstation.

Never run `docker compose down -v` unless you intentionally want to delete all
Neo4j, Qdrant, and PostgreSQL named-volume data. `docker compose down` without
`-v` removes containers but preserves volumes.

## Operations

```powershell
# View logs
docker compose logs -f neo4j

# Stop and start without losing volumes
docker compose stop neo4j
docker compose --profile neo4j start neo4j

# Update image while retaining volumes
docker compose --profile neo4j pull neo4j
docker compose --profile neo4j up -d neo4j

# List Neo4j-related volumes
docker volume ls | Select-String neo4j
```