# graph.json to Neo4j Migration Plan

## Objective

Move the persisted NetworkX snapshot at `data/graph/graph.json` to Neo4j with
zero source-data loss, backward compatibility, incremental reruns, and
verifiable migration evidence.

## Source Analysis

`graph.json` is a directed NetworkX `MultiDiGraph` persisted in node-link JSON
format. Each node has a stable id, type, name, service, and free-form
attributes. Each edge has source, target, type/key, and attributes.

The source snapshot remains the rollback artifact during and after migration.
The existing `KnowledgeGraph` backend continues to work unchanged while
`GRAPH_BACKEND=networkx` is configured.

## Migration Guarantees

- **Zero source-data loss:** the migrator never edits or deletes `graph.json`.
  It creates a checksum-named copy under `data/graph/migrations/` before writes.
- **Attribute fidelity:** node and relationship attributes are stored as exact
  JSON in `attributes_json`, while hot fields are promoted for indexing.
- **Parallel-edge preservation:** each relationship receives a stable `edge_id`
  based on source, target, relationship type, and serialized attributes.
- **Backward compatibility:** NetworkX remains the default until validation
  succeeds and operators explicitly set `GRAPH_BACKEND=neo4j`.
- **Incremental reruns:** `MERGE` on node `id` and relationship `edge_id` makes
  an unchanged snapshot idempotent. A new snapshot checksum produces a new
  migration id and updated source markers.
- **Auditability:** every completed migration writes a JSON report with source
  checksum, counts, timestamps, and validation state.

## Components

| Component | Responsibility |
|---|---|
| `GraphJsonToNeo4jMigrator` | Loads source, backs it up, writes nodes/edges, validates and reports |
| `scripts/migrate_graph_to_neo4j.py` | Operator CLI wrapper |
| `data/graph/migrations/` | Snapshot backups and checksum-scoped migration reports |
| `Neo4jGraphStore` | Existing runtime graph adapter after cutover |

## Runbook

### 1. Preserve and inspect the source graph

```powershell
Copy-Item .\data\graph\graph.json .\data\graph\graph.pre-migration.json
.\.venv\Scripts\python.exe scripts\migrate_graph_to_neo4j.py --dry-run
```

The dry run loads the source and reports expected node/edge totals without
connecting to Neo4j.

### 2. Configure Neo4j credentials

```powershell
$env:NEO4J_URI = "bolt://localhost:7687"
$env:NEO4J_USER = "neo4j"
$env:NEO4J_PASSWORD = "<your-password>"
$env:NEO4J_DATABASE = "neo4j"
```

Keep `GRAPH_BACKEND=networkx` during this stage. The migrator deliberately
loads `KnowledgeGraph` directly so it always reads the JSON source.

### 3. Run migration

```powershell
.\.venv\Scripts\python.exe scripts\migrate_graph_to_neo4j.py --batch-size 500
```

The migrator creates:

```text
data/graph/migrations/
  graph-json-<source-checksum>.graph.json
  graph-json-<source-checksum>.json
```

### 4. Validate counts and key data

The CLI exits nonzero if snapshot-scoped counts do not equal source counts.
Review the generated report, then run direct validation:

```cypher
MATCH (n:KBNode)
RETURN count(n) AS nodeCount;

MATCH (:KBNode)-[r]->(:KBNode)
WHERE r.edge_id IS NOT NULL
RETURN count(r) AS migratedEdgeCount;

MATCH (n:KBNode)
RETURN n.type AS type, count(*) AS count
ORDER BY type;
```

Compare the type distribution with the `by_type` section in the source graph
statistics.

### 5. Dual-read validation

Before cutover, run equivalent representative queries against both backends:

- service dependency map
- endpoint execution flow
- table/entity impact analysis
- Kafka producer/consumer topology
- incident-to-service relationship lookup

Keep `graph.json` as the fallback until results match and Neo4j backup/restore
has been tested.

### 6. Cut over

```powershell
$env:GRAPH_BACKEND = "neo4j"
mcp-kb-server
```

New ingestion writes directly to Neo4j. Continue retaining the JSON snapshot
for rollback until the enterprise operational acceptance period is complete.

## Rollback

Set the backend to NetworkX and restart the ingestion/server process:

```powershell
$env:GRAPH_BACKEND = "networkx"
mcp-kb-server
```

No restore is needed for application rollback because `graph.json` was never
changed. Neo4j remains available for analysis or later retry.

## Incremental Migration Strategy

1. Use `mcp-kb-refresh` or the normal ingestion pipeline to keep the JSON
   snapshot current until cutover.
2. Rerun the migrator after a snapshot change. Existing identities are upserted
   and unchanged records are not duplicated.
3. Review a new checksum-scoped report for every snapshot version.
4. After Neo4j becomes primary, run ingestion with `GRAPH_BACKEND=neo4j`.
   Retain periodic JSON export/snapshot backups until the legacy backend is
   formally retired.
