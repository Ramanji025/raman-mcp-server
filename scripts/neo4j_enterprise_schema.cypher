// MCP Microservices Knowledge Base: Enterprise Neo4j schema
// Neo4j 5.x. Safe to rerun: all constraints/indexes use IF NOT EXISTS.
//
// Runtime compatibility:
// The Python graph store uses :KBNode plus labels from models.NodeType.
// It currently maps the canonical labels below as:
// Repository -> GitRepository, APIEndpoint -> Endpoint,
// DatabaseTable -> Table, KafkaTopic -> Topic.

// Every ingested node has a stable id. This is the platform-wide identity key.
CREATE CONSTRAINT kbnode_id IF NOT EXISTS
FOR (n:KBNode) REQUIRE n.id IS UNIQUE;

// Canonical node identities. Use id for all ingestion MERGE operations.
CREATE CONSTRAINT repository_id IF NOT EXISTS
FOR (n:Repository) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT service_id IF NOT EXISTS
FOR (n:Service) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT package_id IF NOT EXISTS
FOR (n:Package) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT class_id IF NOT EXISTS
FOR (n:Class) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT interface_id IF NOT EXISTS
FOR (n:Interface) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT method_id IF NOT EXISTS
FOR (n:Method) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT api_endpoint_id IF NOT EXISTS
FOR (n:APIEndpoint) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT database_table_id IF NOT EXISTS
FOR (n:DatabaseTable) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT kafka_topic_id IF NOT EXISTS
FOR (n:KafkaTopic) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT exception_id IF NOT EXISTS
FOR (n:Exception) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT incident_id IF NOT EXISTS
FOR (n:Incident) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT business_capability_id IF NOT EXISTS
FOR (n:BusinessCapability) REQUIRE n.id IS UNIQUE;

// Hot query indexes for the generic live graph model.
CREATE INDEX kbnode_type IF NOT EXISTS
FOR (n:KBNode) ON (n.type);
CREATE INDEX kbnode_name IF NOT EXISTS
FOR (n:KBNode) ON (n.name);
CREATE INDEX kbnode_service IF NOT EXISTS
FOR (n:KBNode) ON (n.service);
CREATE INDEX kbnode_file IF NOT EXISTS
FOR (n:KBNode) ON (n.file);

// Canonical-schema indexes.
CREATE INDEX service_name IF NOT EXISTS
FOR (n:Service) ON (n.name);
CREATE INDEX method_name IF NOT EXISTS
FOR (n:Method) ON (n.name);
CREATE INDEX class_name IF NOT EXISTS
FOR (n:Class) ON (n.name);
CREATE INDEX api_endpoint_route IF NOT EXISTS
FOR (n:APIEndpoint) ON (n.http_method, n.path);
CREATE INDEX database_table_name IF NOT EXISTS
FOR (n:DatabaseTable) ON (n.name);
CREATE INDEX kafka_topic_name IF NOT EXISTS
FOR (n:KafkaTopic) ON (n.name);
CREATE INDEX incident_severity_status IF NOT EXISTS
FOR (n:Incident) ON (n.severity, n.status);
CREATE INDEX business_capability_name IF NOT EXISTS
FOR (n:BusinessCapability) ON (n.name);

// Full-text indexes are intended for direct Neo4j Browser/Cypher discovery.
CREATE FULLTEXT INDEX kbnode_search IF NOT EXISTS
FOR (n:KBNode) ON EACH [n.name, n.service, n.attributes_json];
CREATE FULLTEXT INDEX incident_search IF NOT EXISTS
FOR (n:Incident) ON EACH [n.title, n.description, n.root_cause, n.fix_summary];

// Relationship types used by the platform:
// CALLS, IMPLEMENTS, EXTENDS, THROWS, CATCHES, READS, WRITES, USES,
// DEPENDS_ON, PUBLISHES, CONSUMES, BELONGS_TO.
// Neo4j relationship types do not need pre-declaration; they are created by MERGE.

// ── Repository Technology Detection ────────────────────────────────────────
// Technology nodes derived from each repo's own build manifests.
CREATE CONSTRAINT technology_id IF NOT EXISTS
FOR (n:Technology) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT architecture_id IF NOT EXISTS
FOR (n:Architecture) REQUIRE n.id IS UNIQUE;

CREATE INDEX technology_name IF NOT EXISTS
FOR (n:Technology) ON (n.name);

// Composite index: find all concepts belonging to a specific technology.
CREATE INDEX kbnode_technology IF NOT EXISTS
FOR (n:KBNode) ON (n.technology);
