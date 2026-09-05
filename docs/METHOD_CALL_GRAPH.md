# Method-Level Call Graph

## Indexed Model

During ingestion, the Java parser records each non-private service/controller
method and its source-level invocation expressions. The enterprise graph
augmentation then resolves invocations into Neo4j relationships:

```text
APIEndpoint -[:CALLS {via: "endpoint_handler", seq: 1}]-> Controller Method
Controller Method -[:CALLS {via: "method_invocation"}]-> Service Method
Service Method -[:CALLS {via: "method_invocation"}]-> Service Method
Service Method -[:CALLS {via: "method_invocation"}]-> Repository Method
```

For Spring Data calls such as `inventoryRepository.save()`, when no explicit
repository method declaration exists in source, the indexer creates an inferred
`Method` node with `inferred: true` and preserves the link to its Repository.

All method nodes use a stable id:

```text
method:<service>:<ClassName>.<methodName>
```

Example:

```text
endpoint:order-service:POST:/orders
  CALLS
method:order-service:OrderController.submit
  CALLS
method:order-service:OrderService.process
  CALLS
method:order-service:InventoryService.reserve
  CALLS
method:order-service:InventoryRepository.save
```

## Neo4j Persistence

When `GRAPH_BACKEND=neo4j`, the existing graph store writes every generated
`CALLS` edge to Neo4j. The `GraphJsonToNeo4jMigrator` also preserves them during
legacy snapshot migration. Edges include `attributes_json`; method invocation
edges additionally contain resolution metadata such as the call expression,
resolved class, and resolved method.

The following indexes make node-id traversal and class/method resolution fast:

```cypher
CREATE CONSTRAINT kbnode_id IF NOT EXISTS
FOR (n:KBNode) REQUIRE n.id IS UNIQUE;

CREATE INDEX kbnode_class_name IF NOT EXISTS
FOR (n:KBNode) ON (n.class, n.name);
```

## Efficient Cypher Queries

Use stable `id` parameters for the most selective queries. Bounds are required
on variable-length traversals to protect the database from unbounded graph
expansion.

### Find Callers

```cypher
MATCH path = (caller:KBNode)-[:CALLS|DELEGATES_TO*1..8]->
  (target:KBNode:Method {id: $methodId})
WITH caller, min(length(path)) AS hops
RETURN caller.id, caller.type, caller.name, caller.service, hops
ORDER BY hops, caller.service, caller.name
LIMIT 100;
```

### Find Callees

```cypher
MATCH path = (source:KBNode:Method {id: $methodId})-[:CALLS|DELEGATES_TO*1..8]->
  (callee:KBNode)
WITH callee, min(length(path)) AS hops
RETURN callee.id, callee.type, callee.name, callee.service, hops
ORDER BY hops, callee.service, callee.name
LIMIT 100;
```

### Trace Execution Path

```cypher
MATCH path = (source:KBNode {id: $sourceId})
  -[:CALLS|DELEGATES_TO|READS|WRITES|PUBLISHES|CONSUMES*1..8]->
  (target:KBNode {id: $targetId})
RETURN [node IN nodes(path) | {
  id: node.id, type: node.type, name: node.name, service: node.service
}] AS nodes,
[rel IN relationships(path) | {
  type: type(rel), seq: rel.seq, via: rel.via
}] AS relationships
ORDER BY length(path)
LIMIT 10;
```

### Impact Analysis

This reverse traversal identifies nodes whose execution/data/event path reaches
the changed target.

```cypher
MATCH path = (impacted:KBNode)
  -[:CALLS|DELEGATES_TO|READS|WRITES|DEPENDS_ON|PUBLISHES|CONSUMES*1..5]->
  (target:KBNode {id: $targetId})
WITH impacted, min(length(path)) AS hops
RETURN impacted.id, impacted.type, impacted.name, impacted.service, hops
ORDER BY hops, impacted.service, impacted.name
LIMIT 250;
```

## Python API

`Neo4jGraphManager` provides async equivalents for `find_callers`,
`find_callees`, `trace_execution_path`, and `impact_analysis`:

```python
async with Neo4jGraphManager() as graph:
    callers = await graph.find_callers(
        "method:order-service:OrderService.process"
    )
    path = await graph.trace_execution_path(
        "endpoint:order-service:POST:/orders",
        "method:order-service:InventoryRepository.save",
    )
```

Re-index a service or run the full rewrite after deploying this enhancement so
existing snapshots receive the new method-level `CALLS` edges.
