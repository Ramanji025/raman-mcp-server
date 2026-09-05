-- ============================================================
-- Optional PostgreSQL metadata store for the MCP KB.
-- Tracks repositories, files, ingestion runs and per-file hashes
-- used for incremental (change-detection based) re-indexing.
-- ============================================================

CREATE TABLE IF NOT EXISTS repositories (
    id           SERIAL PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    url          TEXT,
    branch       TEXT DEFAULT 'main',
    last_commit  TEXT,
    last_indexed TIMESTAMPTZ,
    -- Version-aware indexing: the release/build last ingested, so tools like
    -- compare_versions() can answer "show implementation in release 2.7".
    release_tag  TEXT,
    build_number TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Idempotent for upgrades of an existing database created before version-aware
-- indexing was added.
ALTER TABLE repositories ADD COLUMN IF NOT EXISTS release_tag TEXT;
ALTER TABLE repositories ADD COLUMN IF NOT EXISTS build_number TEXT;

CREATE TABLE IF NOT EXISTS ingested_files (
    id            SERIAL PRIMARY KEY,
    repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    rel_path      TEXT NOT NULL,
    content_type  TEXT NOT NULL,         -- java | maven | spring_yaml | liquibase | openapi | docs | incidents ...
    sha256        TEXT NOT NULL,         -- content hash for change detection
    chunk_count   INTEGER NOT NULL DEFAULT 0,
    indexed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (repository_id, rel_path)
);

CREATE INDEX IF NOT EXISTS idx_ingested_files_repo ON ingested_files(repository_id);
CREATE INDEX IF NOT EXISTS idx_ingested_files_type ON ingested_files(content_type);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    id             SERIAL PRIMARY KEY,
    repository_id  INTEGER REFERENCES repositories(id) ON DELETE CASCADE,
    run_type       TEXT NOT NULL,        -- full | incremental
    files_added    INTEGER DEFAULT 0,
    files_updated  INTEGER DEFAULT 0,
    files_deleted  INTEGER DEFAULT 0,
    chunks_written INTEGER DEFAULT 0,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at    TIMESTAMPTZ,
    status         TEXT DEFAULT 'running'
);

-- Denormalised copy of graph nodes for SQL-based lookups / joins.
CREATE TABLE IF NOT EXISTS graph_nodes (
    id            TEXT PRIMARY KEY,      -- stable node id, e.g. "endpoint:rx-order:POST /orders"
    node_type     TEXT NOT NULL,
    service       TEXT,
    name          TEXT NOT NULL,
    attributes    JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_graph_nodes_type    ON graph_nodes(node_type);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_service ON graph_nodes(service);

CREATE TABLE IF NOT EXISTS graph_edges (
    id          SERIAL PRIMARY KEY,
    src_id      TEXT NOT NULL,
    dst_id      TEXT NOT NULL,
    edge_type   TEXT NOT NULL,
    attributes  JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (src_id, dst_id, edge_type)
);

CREATE INDEX IF NOT EXISTS idx_graph_edges_src ON graph_edges(src_id);
CREATE INDEX IF NOT EXISTS idx_graph_edges_dst ON graph_edges(dst_id);

-- ============================================================
-- RCA / incident memory (first-class entities — see
-- mcp_kb.rca.incident_store and docs/ENTERPRISE_ARCHITECTURE.md).
-- Also created lazily at runtime if this init script hasn't run yet.
-- ============================================================
CREATE TABLE IF NOT EXISTS incidents (
    id                 TEXT PRIMARY KEY,
    title              TEXT NOT NULL,
    description        TEXT DEFAULT '',
    severity           TEXT NOT NULL DEFAULT 'P3',   -- P1 | P2 | P3 | P4
    status             TEXT NOT NULL DEFAULT 'open',  -- open | investigating | resolved | closed
    service_name       TEXT,
    exception_type     TEXT,
    stack_trace        TEXT,
    root_cause         TEXT,
    confidence         DOUBLE PRECISION DEFAULT 0,
    fix_summary        TEXT,
    related_commit     TEXT,
    affected_services  JSONB DEFAULT '[]'::jsonb,
    metadata           JSONB DEFAULT '{}'::jsonb,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_incidents_service  ON incidents(service_name);
CREATE INDEX IF NOT EXISTS idx_incidents_severity ON incidents(severity);
CREATE INDEX IF NOT EXISTS idx_incidents_status   ON incidents(status);

-- ============================================================
-- Security: audit log for every MCP tool invocation (goal: auditability).
-- Populated by mcp_kb.security.audit_log; append-only by convention.
-- ============================================================
CREATE TABLE IF NOT EXISTS audit_log (
    id           BIGSERIAL PRIMARY KEY,
    principal    TEXT NOT NULL DEFAULT 'local',
    tool         TEXT NOT NULL,
    query        JSONB NOT NULL DEFAULT '{}'::jsonb,
    repo_scope   TEXT,
    allowed      BOOLEAN NOT NULL DEFAULT true,
    denial_reason TEXT,
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_log_principal ON audit_log(principal);
CREATE INDEX IF NOT EXISTS idx_audit_log_tool      ON audit_log(tool);
CREATE INDEX IF NOT EXISTS idx_audit_log_time      ON audit_log(occurred_at);
