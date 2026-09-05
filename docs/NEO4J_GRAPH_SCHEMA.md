# Enterprise Neo4j Graph Schema

## Purpose

This schema models the MCP platform as a connected engineering knowledge graph.
It supports 44 microservices, shared libraries, REST APIs, Kafka, relational
data, exception flow, incidents/RCA, and business capability analysis.

The design uses stable node identity, explicit relationship semantics, and
indexes matched to the queries used by code search, dependency analysis, impact
analysis, execution-flow discovery, incident intelligence, and version-aware
engineering documentation.

## Runtime Model and Canonical Labels

The live Python graph store uses a generic `:KBNode` label and adds a dynamic
label from `NodeType`. All nodes have these common properties:

```cypher
(:KBNode {
  id: "stable platform-wide identifier",
  type: "runtime node type",
  name: "display name",
  service: "owning microservice when applicable",
  file: "source relative path when applicable",
  start_line: 1,
  end_line: 20,
  attributes_json: "full original attributes as JSON"
})
```

The requested canonical schema uses the following labels. The mapping column
shows the labels currently emitted by the platform.

| Canonical label | Meaning | Current runtime label |
|---|---|---|
| `Repository` | Git repository / source repository | `GitRepository` |
| `Service` | Microservice, gateway, or independently deployed service | `Service` or `Gateway` |
| `Package` | Java package | `Package` |
| `Class` | Concrete Java class | `Class`, `Controller`, `ServiceLayer`, `Entity`, or `Repository` |
| `Interface` | Java interface / client contract | `Interface` |
| `Method` | Method or callable symbol | `Method` |
| `APIEndpoint` | HTTP endpoint | `Endpoint` |
| `DatabaseTable` | Relational table | `Table` |
| `KafkaTopic` | Kafka topic | `Topic` |
| `Exception` | Exception type or runtime exception | `ExceptionType` or `Exception` |
| `Incident` | Persisted exception/RCA incident | `Incident` |
| `BusinessCapability` | Business function supported by technical flow | `BusinessCapability` |

Do not relabel existing data solely for naming cosmetics. Use the runtime labels
for MCP ingestion and tools; use the canonical names in architecture diagrams,
reporting, and new integrations. The aliases above preserve compatibility with
already indexed graph data.

## Node Types and Properties

### Repository

```cypher
(:Repository {
  id, name, remote_url, default_branch, commit, version, owner_team
})
```

Represents an independently versioned source repository, including shared
libraries. A shared library is also a repository; it may have `kind: "library"`
in `attributes_json` or connect to a `Dependency` node.

### Service

```cypher
(:Service {
  id, name, domain, owner_team, runtime, version, repository_id
})
```

Represents a deployable microservice. The API Gateway is modeled as
`:Service:Gateway` and calls services through `:CALLS` relationships.

### Package, Class, Interface, Method

```cypher
(:Package {id, name, service, file})
(:Class {id, name, fqcn, service, file, start_line, end_line})
(:Interface {id, name, fqcn, service, file})
(:Method {id, name, signature, service, file, start_line, end_line})
```

Methods use a stable identifier that includes service, class/FQCN, signature,
and file location. This prevents collisions between common method names such as
`save`, `getById`, and `process`.

### APIEndpoint

```cypher
(:APIEndpoint {
  id, name, service, http_method, path, handler, auth, request_type, response_type
})
```

Use the combined HTTP method and normalized path as a natural lookup key, but
keep `id` as the immutable graph identity.

### DatabaseTable and KafkaTopic

```cypher
(:DatabaseTable {id, name, schema, database, service})
(:KafkaTopic {id, name, cluster, retention, schema_version})
```

A table can be shared by multiple services. A topic can have multiple producers
and consumers; model each relationship separately and place operational
metadata on the relationship where available.

### Exception and Incident

```cypher
(:Exception {id, name, fqcn, service})
(:Incident {
  id, title, severity, status, service_name, exception_type,
  root_cause, confidence, fix_summary, created_at, resolved_at
})
```

An `Incident` is the operational observation and RCA record. `Exception` is a
code/runtime exception concept. Keep them separate so one exception type can
be associated with multiple incidents across versions.

### BusinessCapability

```cypher
(:BusinessCapability {id, name, description, domain, criticality, owner_team})
```

Examples include Order Processing, Payment Processing, Inventory Management,
Customer Onboarding, and Notification Delivery.

## Relationship Types

| Relationship | Source -> Target | Meaning | Common properties |
|---|---|---|---|
| `BELONGS_TO` | package/class/service -> repository; class -> package; method -> class | Ownership/containment | `role`, `since_version` |
| `CALLS` | gateway/service/method/endpoint -> service/method/endpoint | REST, Feign, HTTP, or in-process invocation | `protocol`, `http_method`, `path`, `seq`, `timeout_ms` |
| `IMPLEMENTS` | class -> interface | Java implementation | `file` |
| `EXTENDS` | class -> class; exception -> exception | Inheritance | `file` |
| `THROWS` | method/endpoint -> exception | Declared or inferred throw path | `conditional`, `source_line` |
| `CATCHES` | method -> exception | Exception handling | `source_line`, `action` |
| `READS` | method/repository -> database table | Database read dependency | `query_type`, `tables`, `source_line` |
| `WRITES` | method/repository -> database table | Database write dependency | `operation`, `transactional`, `source_line` |
| `USES` | service/class/method -> library/config/DTO/entity | General code or configuration usage | `usage_kind`, `file` |
| `DEPENDS_ON` | service/repository -> service/library/database | Deployment/build/runtime dependency | `scope`, `version`, `evidence` |
| `PUBLISHES` | service/method -> Kafka topic | Produces an event | `event_type`, `schema_version`, `seq` |
| `CONSUMES` | Kafka topic -> service/method | Consumes an event | `event_type`, `consumer_group`, `seq` |

For incidents and capabilities, the existing platform also uses `AFFECTS` and
capability-support edges. Preserve them because they are important semantic
links beyond the required relationship list.

## Cypher: Schema Installation

Run the idempotent script after Neo4j starts:

```powershell
Get-Content .\scripts\neo4j_enterprise_schema.cypher -Raw | `
  docker exec -i mcpkb-neo4j cypher-shell -u neo4j -p $env:NEO4J_PASSWORD
```

The script creates a unique `KBNode.id` constraint plus indexes for the
platform's current runtime labels and the canonical enterprise model.

## Cypher: Example Writes

### Create a repository, service, package, class, and method

```cypher
MERGE (repo:KBNode:GitRepository {id: 'repo:order-service'})
SET repo.type = 'GitRepository', repo.name = 'order-service',
    repo.service = 'order-service'
MERGE (service:KBNode:Service {id: 'service:order-service'})
SET service.type = 'Service', service.name = 'order-service',
    service.service = 'order-service'
MERGE (pkg:KBNode:Package {id: 'package:order-service:com.example.order'})
SET pkg.type = 'Package', pkg.name = 'com.example.order', pkg.service = 'order-service'
MERGE (cls:KBNode:Class {id: 'class:order-service:OrderService'})
SET cls.type = 'Class', cls.name = 'OrderService', cls.service = 'order-service'
MERGE (method:KBNode:Method {id: 'method:order-service:OrderService:placeOrder'})
SET method.type = 'Method', method.name = 'placeOrder', method.service = 'order-service'
MERGE (service)-[:BELONGS_TO]->(repo)
MERGE (pkg)-[:BELONGS_TO]->(repo)
MERGE (cls)-[:BELONGS_TO]->(pkg)
MERGE (method)-[:BELONGS_TO]->(cls);
```

### Model an API call, database write, and Kafka event

```cypher
MATCH (order:KBNode:Service {id: 'service:order-service'})
MATCH (method:KBNode:Method {id: 'method:order-service:OrderService:placeOrder'})
MERGE (api:KBNode:Endpoint {id: 'endpoint:order-service:POST:/orders'})
SET api.type = 'Endpoint', api.name = 'POST /orders', api.service = 'order-service',
    api.http_method = 'POST', api.path = '/orders'
MERGE (table:KBNode:Table {id: 'table:orders'})
SET table.type = 'Table', table.name = 'orders', table.service = 'order-service'
MERGE (topic:KBNode:Topic {id: 'topic:order-created'})
SET topic.type = 'Topic', topic.name = 'order-created'
MERGE (api)-[:CALLS {protocol: 'http', seq: 1}]->(method)
MERGE (method)-[:WRITES {operation: 'INSERT', transactional: true, seq: 2}]->(table)
MERGE (method)-[:PUBLISHES {event_type: 'OrderCreated', seq: 3}]->(topic)
MERGE (order)-[:PUBLISHES {event_type: 'OrderCreated'}]->(topic);
```

### Model an exception and RCA incident

```cypher
MATCH (method:KBNode:Method {id: 'method:order-service:OrderService:placeOrder'})
MERGE (exception:KBNode:ExceptionType {id: 'exception:InsufficientInventoryException'})
SET exception.type = 'ExceptionType', exception.name = 'InsufficientInventoryException',
    exception.service = 'inventory-service'
MERGE (incident:KBNode:Incident {id: 'incident:INC-1042'})
SET incident.type = 'Incident', incident.name = 'INC-1042',
    incident.severity = 'P2', incident.status = 'resolved',
    incident.root_cause = 'Inventory reservation timeout',
    incident.fix_summary = 'Added timeout and retry policy'
MERGE (method)-[:THROWS]->(exception)
MERGE (incident)-[:AFFECTS]->(method)
MERGE (incident)-[:AFFECTS]->(exception);
```

## Cypher: Retrieval and Analysis Examples

### Service dependency map

```cypher
MATCH (source:KBNode:Service)-[r:CALLS|DEPENDS_ON]->(target:KBNode:Service)
RETURN source.name AS source, type(r) AS relationship, target.name AS target
ORDER BY source, target;
```

### Find all callers of a method

```cypher
MATCH path = (caller:KBNode)-[:CALLS|DELEGATES_TO*1..6]->
  (target:KBNode:Method {id: $methodId})
RETURN caller.name AS caller, caller.service AS service,
       [node IN nodes(path) | node.name] AS path
ORDER BY size(nodes(path));
```

### Trace an API execution path with sequence numbers

```cypher
MATCH path = (api:KBNode:Endpoint {id: $endpointId})-[rels:CALLS|DELEGATES_TO|WRITES|PUBLISHES*1..8]->(target)
WITH api, target, rels,
     reduce(total = 0, rel IN rels | total + coalesce(rel.seq, 0)) AS sequenceScore
RETURN api.name, target.name,
       [rel IN rels | {type: type(rel), seq: rel.seq}] AS steps
ORDER BY sequenceScore;
```

### Database change impact

```cypher
MATCH (table:KBNode:Table {name: $tableName})<-[:READS|WRITES*1..5]-(upstream:KBNode)
RETURN DISTINCT upstream.type, upstream.name, upstream.service
ORDER BY upstream.service, upstream.name;
```

### Kafka producer-to-consumer topology

```cypher
MATCH (producer:KBNode)-[:PUBLISHES|PRODUCES_TO]->(topic:KBNode:Topic)<-[:CONSUMES|CONSUMES_FROM]-(consumer:KBNode)
RETURN producer.service AS producerService, topic.name AS topic,
       consumer.service AS consumerService
ORDER BY topic, producerService, consumerService;
```

### Incident history for a service

```cypher
MATCH (incident:KBNode:Incident)-[:AFFECTS]->(node:KBNode {service: $service})
RETURN incident.name, incident.severity, incident.status,
       incident.root_cause, incident.fix_summary
ORDER BY incident.created_at DESC;
```

### Business capability implementation map

```cypher
MATCH (capability:KBNode:BusinessCapability {name: $capability})-[*1..4]->(node:KBNode)
RETURN DISTINCT node.type, node.name, node.service
ORDER BY node.service, node.type, node.name;
```

### Full-text architecture lookup

```cypher
CALL db.index.fulltext.queryNodes('kbnode_search', $query)
YIELD node, score
RETURN node.type, node.name, node.service, score
ORDER BY score DESC
LIMIT 25;
```

## Indexing Strategy

1. Use the unique `:KBNode(id)` constraint as the write identity for every
   ingested object. All ingestion writes should `MERGE` on `id`.
2. Use exact range indexes on `type`, `service`, `name`, and `file` for the MCP
   graph store's node resolution, service scoping, file replacement, and impact
   traversal.
3. Use composite route index `(http_method, path)` when directly querying the
   canonical `:APIEndpoint` label. The current runtime stores equivalent data in
   `attributes_json`; promote hot fields to top-level properties before relying
   on large-scale Cypher route queries.
4. Use indexes on incident severity/status and capability name for operational
   and portfolio views.
5. Use full-text indexes for Browser/admin discovery. The MCP serving path uses
   Qdrant for semantic search, so do not replace Qdrant with Neo4j full text.
6. Do not create indexes for every label/property combination. Monitor query
   plans with `PROFILE`, index only repeated selective predicates, and retain
   the generic indexes for shared code paths.

## Migration Order

1. Start Neo4j and verify Browser, Bolt, and APOC.
2. Install the optional Python dependency: `pip install -e ".[neo4j]"`.
3. Set `NEO4J_*` credentials for the shell.
4. Run `scripts/neo4j_enterprise_schema.cypher`.
5. Run `scripts/migrate_graph_to_neo4j.py` and require matching node/edge counts.
6. Set `GRAPH_BACKEND=neo4j` for future ingestion and MCP serving.
7. Retain `data/graph/graph.json` as a rollback snapshot until Neo4j validation
   and backup/restore tests are complete.
