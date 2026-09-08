"""Centralized, environment-driven configuration.

Values come from environment variables (see ``.env.example``) with sane
local-first defaults, plus static tuning loaded from ``config/config.yaml``.
"""
from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings resolved from the process environment / ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        case_sensitive=False,
    )

    # ---- Server ----
    env: str = Field("local", alias="MCP_KB_ENV")
    log_level: str = Field("INFO", alias="MCP_KB_LOG_LEVEL")
    transport: str = Field("stdio", alias="MCP_KB_TRANSPORT")
    http_host: str = Field("127.0.0.1", alias="MCP_KB_HTTP_HOST")
    http_port: int = Field(8000, alias="MCP_KB_HTTP_PORT")

    # ---- CORS (HTTP transport only) ----
    # Comma-separated origins.  Use "*" for local dev; set explicit origins in prod.
    # e.g.  CORS_ALLOWED_ORIGINS=http://localhost:3000,https://myapp.example.com
    cors_allowed_origins: str = Field("*", alias="CORS_ALLOWED_ORIGINS")
    cors_allow_credentials: bool = Field(False, alias="CORS_ALLOW_CREDENTIALS")
    cors_allowed_methods: str = Field("*", alias="CORS_ALLOWED_METHODS")
    cors_allowed_headers: str = Field("*", alias="CORS_ALLOWED_HEADERS")

    @property
    def cors_origins_list(self) -> list[str]:
        """Parse CORS_ALLOWED_ORIGINS into a Python list."""
        raw = self.cors_allowed_origins.strip()
        return ["*"] if not raw or raw == "*" else [o.strip() for o in raw.split(",") if o.strip()]

    @property
    def cors_methods_list(self) -> list[str]:
        """Parse CORS_ALLOWED_METHODS into a Python list."""
        raw = self.cors_allowed_methods.strip()
        return ["*"] if not raw or raw == "*" else [m.strip() for m in raw.split(",") if m.strip()]

    @property
    def cors_headers_list(self) -> list[str]:
        """Parse CORS_ALLOWED_HEADERS into a Python list."""
        raw = self.cors_allowed_headers.strip()
        return ["*"] if not raw or raw == "*" else [h.strip() for h in raw.split(",") if h.strip()]

    # ---- Repositories ----
    repos_root: Path = Field(Path("./data/repos"), alias="MCP_KB_REPOS_ROOT")
    repos_manifest: Path = Field(Path("./config/repos.yaml"), alias="MCP_KB_REPOS_MANIFEST")
    # HTTPS auth for private git remotes (e.g. GitHub/GitLab/Bitbucket PAT).
    git_username: str | None = Field(None, alias="GIT_USERNAME")
    git_token: str | None = Field(None, alias="GIT_TOKEN")
    git_default_branch: str = Field("main", alias="GIT_DEFAULT_BRANCH")
    # When true (e.g. on a cloud VM with its own credential helper/SSH agent),
    # skip embedding GIT_USERNAME/GIT_TOKEN into clone URLs and use plain URLs.
    git_use_system_credentials: bool = Field(False, alias="GIT_USE_SYSTEM_CREDENTIALS")

    # ---- Qdrant ----
    qdrant_url: str = Field("http://localhost:6333", alias="QDRANT_URL")
    qdrant_api_key: str | None = Field(None, alias="QDRANT_API_KEY")
    qdrant_collection_prefix: str = Field("mcpkb", alias="QDRANT_COLLECTION_PREFIX")
    # Follow-on gap 1 perf: gRPC transport is materially faster than REST for
    # bulk upsert/search (binary protocol, HTTP/2 multiplexing). Requires the
    # gRPC port (6334 by default) to be reachable; falls back cleanly if not.
    qdrant_prefer_grpc: bool = Field(False, alias="QDRANT_PREFER_GRPC")
    qdrant_grpc_port: int = Field(6334, alias="QDRANT_GRPC_PORT")
    # upload_points batching/parallelism (qdrant-client's own high-throughput
    # bulk-upload primitive — replaces a single blocking client.upsert() call).
    qdrant_upsert_batch_size: int = Field(256, alias="QDRANT_UPSERT_BATCH_SIZE")
    qdrant_upsert_parallel: int = Field(2, alias="QDRANT_UPSERT_PARALLEL")
    # wait=False acks as soon as the write is durable in the WAL, without
    # blocking for the HNSW index to finish updating — much faster bulk
    # ingestion throughput; set true only if a caller needs read-your-writes
    # consistency immediately after upsert (rare — see upsert()'s `wait` arg).
    qdrant_upsert_wait: bool = Field(False, alias="QDRANT_UPSERT_WAIT")
    # Keep full vectors in RAM (fastest) by default; set true only when the
    # index no longer fits in memory (trades query latency for footprint).
    qdrant_on_disk_vectors: bool = Field(False, alias="QDRANT_ON_DISK_VECTORS")

    # ---- Embeddings ----
    embedding_provider: str = Field("fastembed", alias="EMBEDDING_PROVIDER")
    # P1.2: bge-base-en-v1.5 (768-dim) — re-ingest required when changing this
    embedding_model: str = Field("BAAI/bge-base-en-v1.5", alias="EMBEDDING_MODEL")
    embedding_dim: int = Field(768, alias="EMBEDDING_DIM")
    embedding_batch_size: int = Field(64, alias="EMBEDDING_BATCH_SIZE")

    # ---- LLM ----
    # provider: openai | azure | ollama (vendor-independent — swap without code changes)
    llm_provider: str = Field("ollama", alias="LLM_PROVIDER")
    # Some llama.cpp/Ollama servers validate this field against the loaded model
    # name, so set it in .env to match; falls back to "default" if unset.
    llm_model: str = Field("", alias="LLM_MODEL")
    openai_api_key: str | None = Field(None, alias="OPENAI_API_KEY")
    openai_base_url: str | None = Field(None, alias="OPENAI_BASE_URL")
    # Ollama exposes an OpenAI-compatible endpoint; point base_url here for
    # fully local, vendor-independent LLMs (e.g. gemma4:26b).
    ollama_base_url: str = Field("http://localhost:8080/v1", alias="OLLAMA_BASE_URL")
    # Optional overrides for the offline enrichment scripts (llm_enrichment.py,
    # llm_enrich_pilot.py) — leave unset to reuse llm_model / ollama_base_url.
    ollama_model: str | None = Field(None, alias="OLLAMA_MODEL")
    # Path appended to OLLAMA_BASE_URL to build the chat-completions endpoint.
    # Change this in .env if a gateway/proxy exposes it under a different path.
    ollama_generate_url: str = Field("/chat/completions", alias="OLLAMA_GENERATE_URL")
    # Single source of truth for LLM request parallelism across the whole app
    # (concept extraction, discovery, enrichment, RAG synthesis). Match this to
    # the local LLM server's --parallel slot count to avoid 500s from overload.
    llm_max_concurrency: int = Field(4, alias="LLM_MAX_CONCURRENCY")

    @property
    def chat_completions_url(self) -> str:
        """Full POST endpoint for OpenAI-compatible chat completions.

        Composed from ``OLLAMA_BASE_URL`` + ``OLLAMA_GENERATE_URL`` (both from
        .env), so changing either in .env is enough — no source edits needed.
        """
        path = self.ollama_generate_url if self.ollama_generate_url.startswith("/") else f"/{self.ollama_generate_url}"
        base = self.openai_base_url or self.ollama_base_url or "https://api.openai.com/v1"
        return f"{base.rstrip('/')}{path}"

    @property
    def effective_llm_model(self) -> str:
        """Model name to send in chat-completion requests: ``LLM_MODEL`` from .env, else "default"."""
        return self.llm_model or "default"

    # Native Ollama API base (without /v1) — derived automatically
    @property
    def ollama_native_url(self) -> str:
        """http://localhost:11434 — strips /v1 suffix if present."""
        base = self.ollama_base_url
        return base[:-3] if base.endswith("/v1") else base.rstrip("/")

    # ---- Postgres ----
    postgres_enabled: bool = Field(True, alias="POSTGRES_ENABLED")
    postgres_dsn: str = Field(
        "postgresql://mcpkb:mcpkb@localhost:5432/mcpkb", alias="POSTGRES_DSN"
    )

    # ---- Graph backend ----
    # Neo4j is the required runtime graph backend. NetworkX is retained only
    # by the one-time legacy graph.json migration utility.
    graph_backend: str = Field("neo4j", alias="GRAPH_BACKEND")
    neo4j_uri: str = Field("bolt://localhost:7687", alias="NEO4J_URI")
    neo4j_user: str = Field("neo4j", alias="NEO4J_USER")
    neo4j_password: str = Field("neo4j", alias="NEO4J_PASSWORD")
    neo4j_auth_enabled: bool = Field(True, alias="NEO4J_AUTH_ENABLED")
    neo4j_database: str = Field("neo4j", alias="NEO4J_DATABASE")

    @property
    def neo4j_auth(self) -> tuple[str, str] | None:
        """Return credentials only when Neo4j authentication is enabled."""
        return (self.neo4j_user, self.neo4j_password) if self.neo4j_auth_enabled else None

    # ---- Sparse lexical retrieval (Qdrant BM25) ----
    sparse_enabled: bool = Field(True, alias="SPARSE_ENABLED")
    sparse_model: str = Field("Qdrant/bm25", alias="SPARSE_MODEL")

    # ---- Query-time web gap-fill (never overrides project facts) ----
    web_search_enabled: bool = Field(True, alias="WEB_SEARCH_ENABLED")
    web_search_min_confidence: float = Field(0.45, alias="WEB_SEARCH_MIN_CONFIDENCE")
    tavily_api_key: str | None = Field(None, alias="TAVILY_API_KEY")
    # Official Brave Search API (https://brave.com/search/api/) — free tier is
    # 2,000 queries/month, no scraping/captchas. Preferred over ddgs when set.
    brave_api_key: str | None = Field(None, alias="BRAVE_API_KEY")
    # Self-hosted, open-source SearXNG metasearch (free, no API key). Start it
    # via `docker compose up searxng` and it will be queried before ddgs
    # scraping. See config/searxng/settings.yml.
    searxng_url: str | None = Field(None, alias="SEARXNG_URL")

    # ---- Reranking (enterprise RAG precision stage) ----
    # Disabled by default so behaviour/perf is unchanged until a model is chosen.
    rerank_enabled: bool = Field(True, alias="RERANK_ENABLED")
    # provider: cross-encoder (sentence-transformers, local) | noop
    rerank_provider: str = Field("cross-encoder", alias="RERANK_PROVIDER")
    rerank_model: str = Field(
        "BAAI/bge-reranker-v2-m3", alias="RERANK_MODEL"
    )
    rerank_candidate_k: int = Field(30, alias="RERANK_CANDIDATE_K")

    # ---- Observability ----
    otel_enabled: bool = Field(False, alias="OTEL_ENABLED")
    otel_service_name: str = Field("mcp-microservices-kb", alias="OTEL_SERVICE_NAME")
    otel_exporter: str = Field("console", alias="OTEL_EXPORTER")  # console | otlp
    otel_exporter_endpoint: str | None = Field(None, alias="OTEL_EXPORTER_ENDPOINT")

    # ---- Security: RBAC / audit / sensitive-data ----
    rbac_enabled: bool = Field(False, alias="RBAC_ENABLED")
    rbac_policy_path: Path = Field(Path("./config/rbac.yaml"), alias="RBAC_POLICY_PATH")
    audit_log_enabled: bool = Field(True, alias="AUDIT_LOG_ENABLED")
    sensitive_scan_enabled: bool = Field(True, alias="SENSITIVE_SCAN_ENABLED")

    # ---- Phase 1: agent-facing tool profiles (ALL | ANALYSIS | SCOUT) ----
    # Restricts which tools a session/client sees, independent of RBAC's
    # per-repo policy. See security/tool_profiles.py for the tool sets.
    tool_profile: str = Field("ALL", alias="MCP_KB_TOOL_PROFILE")

    # ---- Phase 1: response pagination / size caps (token-budget discipline) ----
    tool_list_default_limit: int = Field(50, alias="MCP_KB_LIST_DEFAULT_LIMIT")
    tool_list_max_limit: int = Field(500, alias="MCP_KB_LIST_MAX_LIMIT")

    # ---- Phase 1: ADR (Architecture Decision Record) storage ----
    adr_store_dir: Path = Field(Path("./data/adr"), alias="MCP_KB_ADR_DIR")

    # ---- Phase 2: graph enrichment passes (post-ingestion, cross-cutting) ----
    similarity_enabled: bool = Field(True, alias="SIMILARITY_ENABLED")
    similarity_minhash_k: int = Field(32, alias="SIMILARITY_MINHASH_K")
    similarity_lsh_bands: int = Field(8, alias="SIMILARITY_LSH_BANDS")
    similarity_jaccard_threshold: float = Field(0.4, alias="SIMILARITY_JACCARD_THRESHOLD")

    semantic_bridge_enabled: bool = Field(False, alias="SEMANTIC_BRIDGE_ENABLED")
    semantic_bridge_min_cosine: float = Field(0.82, alias="SEMANTIC_BRIDGE_MIN_COSINE")
    semantic_bridge_max_methods: int = Field(1500, alias="SEMANTIC_BRIDGE_MAX_METHODS")

    git_coupling_enabled: bool = Field(True, alias="GIT_COUPLING_ENABLED")
    git_coupling_min_commits: int = Field(3, alias="GIT_COUPLING_MIN_COMMITS")
    git_coupling_min_score: float = Field(0.3, alias="GIT_COUPLING_MIN_SCORE")
    git_coupling_lookback_days: int = Field(180, alias="GIT_COUPLING_LOOKBACK_DAYS")

    infra_parsing_enabled: bool = Field(True, alias="INFRA_PARSING_ENABLED")

    # ---- Phase 3: continuous/incremental indexing ----
    # Background thread that periodically `git pull`s + incrementally
    # re-indexes every known repo. Off by default — enable for long-running
    # server deployments; one-shot CLI ingestion is unaffected either way.
    watcher_enabled: bool = Field(False, alias="WATCHER_ENABLED")
    watcher_interval_minutes: int = Field(15, alias="WATCHER_INTERVAL_MINUTES")
    # Auto-index repos that appear under MCP_KB_REPOS_ROOT but were never
    # ingested (e.g. cloned manually), bounded by file count for safety.
    auto_index_enabled: bool = Field(False, alias="AUTO_INDEX_ENABLED")
    auto_index_limit: int = Field(20000, alias="AUTO_INDEX_LIMIT")

    # ---- Phase 3: portable graph snapshot export/import ----
    snapshot_dir: Path = Field(Path("./data/snapshots"), alias="MCP_KB_SNAPSHOT_DIR")

    # ---- Phase 7: local graph visualization UI ----
    # Off by default; auto-started by server.py's main() when enabled, or run
    # standalone via `mcp-kb-ui` / `python -m mcp_kb.ui.graph_viewer`.
    graph_ui_enabled: bool = Field(False, alias="GRAPH_UI_ENABLED")
    graph_ui_port: int = Field(8765, alias="GRAPH_UI_PORT")

    # ---- Security: prompt-injection guardrail (retrieved context → LLM) ----
    prompt_guard_enabled: bool = Field(True, alias="PROMPT_GUARD_ENABLED")
    # mode: sanitize (neutralize + keep chunk) | drop (exclude flagged chunk)
    prompt_guard_mode: str = Field("sanitize", alias="PROMPT_GUARD_MODE")

    # ---- LLM observability (Langfuse) ----
    langfuse_enabled: bool = Field(False, alias="LANGFUSE_ENABLED")
    langfuse_public_key: str | None = Field(None, alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: str | None = Field(None, alias="LANGFUSE_SECRET_KEY")
    langfuse_host: str = Field("http://localhost:3000", alias="LANGFUSE_HOST")

    # ---- Online learning from usage (aliases, retrieval boosts, route memory) ----
    learning_enabled: bool = Field(True, alias="LEARNING_ENABLED")
    # backend: file (JSON, default, zero infra) | neo4j_qdrant (durable graph + payload boosts)
    learning_backend: str = Field("file", alias="LEARNING_BACKEND")

    # ---- Phase 2: LightGBM ranker training (offline, local, no external ML service) ----
    learning_model_path: Path = Field(Path("./data/models/ranker.txt"), alias="LEARNING_MODEL_PATH")
    learning_min_training_rows: int = Field(30, alias="LEARNING_MIN_TRAINING_ROWS")
    learning_model_min_auc: float = Field(0.55, alias="LEARNING_MODEL_MIN_AUC")
    learning_model_boost_cap: float = Field(0.5, alias="LEARNING_MODEL_BOOST_CAP")

    # ---- Phase 3: scheduled (re)training cadence ----
    learning_train_enabled: bool = Field(False, alias="LEARNING_TRAIN_ENABLED")
    learning_train_interval_minutes: int = Field(1, alias="LEARNING_TRAIN_INTERVAL_MINUTES")

    # ---- Learning dashboard (Plotly Dash UI) ----
    learning_dashboard_port: int = Field(8060, alias="LEARNING_DASHBOARD_PORT")
    learning_dashboard_refresh_s: int = Field(15, alias="LEARNING_DASHBOARD_REFRESH_S")
    # Auto-start the cron trainer / dashboard as background threads when the
    # MCP server (`mcp-kb-server`) boots, instead of running the scripts by hand.
    learning_dashboard_autostart: bool = Field(False, alias="LEARNING_DASHBOARD_AUTOSTART")

    # ---- RCA / incidents ----
    incident_similarity_top_k: int = Field(5, alias="INCIDENT_SIMILARITY_TOP_K")

    # ---- Ingestion ----
    ingest_max_file_size_kb: int = Field(1024, alias="INGEST_MAX_FILE_SIZE_KB")
    ingest_code_chunk_lines: int = Field(120, alias="INGEST_CODE_CHUNK_LINES")
    ingest_doc_chunk_tokens: int = Field(512, alias="INGEST_DOC_CHUNK_TOKENS")
    ingest_concurrency: int = Field(8, alias="INGEST_CONCURRENCY")

    # ---- Rally / CA Agile Central ----
    rally_api_key: str | None = Field(None, alias="RALLY_API_KEY")
    rally_base_url: str = Field(
        "https://rally1.rallydev.com/slm/webservice/v2.0", alias="RALLY_BASE_URL"
    )
    rally_workspace_ref: str | None = Field(None, alias="RALLY_WORKSPACE_REF")
    rally_project_ref: str | None = Field(None, alias="RALLY_PROJECT_REF")
    # Secure by default (Phase 6). Only set false for a trusted internal Rally
    # instance with a self-signed cert you cannot add to the CA bundle.
    rally_verify_ssl: bool = Field(True, alias="RALLY_VERIFY_SSL")

    # ---- Static config path ----
    config_yaml: Path = Field(Path("./config/config.yaml"))

    @functools.cached_property
    def static(self) -> dict[str, Any]:
        """Parsed ``config/config.yaml`` (ingestion/vector/graph tuning)."""
        path = self.config_yaml
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}

    def collection_name(self, content_type: str) -> str:
        """Qualified Qdrant collection name for a content type."""
        return f"{self.qdrant_collection_prefix}_{content_type}"


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide singleton settings instance."""
    return Settings()
