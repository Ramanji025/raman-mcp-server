#!/usr/bin/env bash
# Start the MCP Microservices Knowledge Base on macOS WITHOUT Docker.
#
# This installs and runs each component natively:
#   - PostgreSQL   (Homebrew service)
#   - Neo4j        (Homebrew service, with APOC)
#   - Qdrant       (official macOS binary, run in the background)
#   - MCP server   (Python venv, run in the background)
#
# Open WebUI is NOT installed by this script (it requires Python < 3.13). Run it
# separately and point it at the MCP endpoint printed at the end.
# Ollama is optional and only needed for --build-knowledge LLM enrichment.

set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_KNOWLEDGE="false"
SKIP_OLLAMA_ENRICHMENT="false"
SKIP_INSTALL="false"
PYTHON_BIN="${PYTHON_BIN:-python3}"

usage() {
  cat <<'EOF'
Usage:
  ./scripts/start-native-stack-mac.sh [options]

Installs and starts every component natively on macOS (no Docker required):
PostgreSQL, Neo4j, Qdrant, and the MCP server. Open WebUI is run separately.

Options:
  --build-knowledge           Run the full knowledge rewrite after startup.
  --skip-ollama-enrichment    With --build-knowledge, skip the offline Ollama pass.
  --skip-install              Assume Homebrew formulae are already installed; only
                              start services and the app.
  --python PATH               Python interpreter for the venv (default: python3).
  --help                      Show this help.

Examples:
  ./scripts/start-native-stack-mac.sh
  ./scripts/start-native-stack-mac.sh --build-knowledge
  ./scripts/start-native-stack-mac.sh --skip-install
EOF
}

log() { printf '\n[setup] %s\n' "$*"; }
warn() { printf '\n[warning] %s\n' "$*" >&2; }
die() { printf '\n[error] %s\n' "$*" >&2; exit 1; }

on_error() {
  local status=$?
  printf '\n[error] Setup stopped at line %s (exit %s).\n' "$1" "$status" >&2
  printf '[error] Review the resolution shown above or inspect service logs.\n' >&2
  exit "$status"
}
trap 'on_error $LINENO' ERR

while (($#)); do
  case "$1" in
    --build-knowledge) BUILD_KNOWLEDGE="true" ;;
    --skip-ollama-enrichment) SKIP_OLLAMA_ENRICHMENT="true" ;;
    --skip-install) SKIP_INSTALL="true" ;;
    --python)
      shift
      (($#)) || die "--python requires an executable path."
      PYTHON_BIN="$1"
      ;;
    --help|-h) usage; exit 0 ;;
    *) die "Unknown option: $1. Run with --help for usage." ;;
  esac
  shift
done

[[ "$(uname -s)" == "Darwin" ]] || die "This script targets macOS. Use scripts/start-openwebui-stack.sh on Linux."

cd "$PROJECT_ROOT"
mkdir -p data/logs

check_command() {
  command -v "$1" >/dev/null 2>&1 || die "$2"
}

check_http() {
  curl --fail --silent --show-error --max-time 5 "$1" >/dev/null
}

port_open() {
  local host="$1" port="$2"
  "$PROJECT_ROOT/.venv/bin/python" - "$host" "$port" <<'PY'
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
try:
    with socket.create_connection((host, port), timeout=2):
        pass
except OSError:
    raise SystemExit(1)
PY
}

wait_for_http() {
  local url="$1" name="$2" tries="${3:-30}"
  local i=0
  until check_http "$url"; do
    ((i += 1))
    ((i >= tries)) && die "$name did not become reachable at $url after $((tries * 2))s. Check its logs."
    sleep 2
  done
}

# ---------------------------------------------------------------------------
# Homebrew
# ---------------------------------------------------------------------------
ensure_homebrew() {
  if ! command -v brew >/dev/null 2>&1; then
    die "Homebrew is required. Install it from https://brew.sh, then rerun this script."
  fi
}

brew_install() {
  local formula="$1"
  if brew list --formula "$formula" >/dev/null 2>&1; then
    log "$formula already installed"
  else
    log "Installing $formula with Homebrew"
    brew install "$formula"
  fi
}

# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------
start_postgres() {
  [[ "$SKIP_INSTALL" == "true" ]] || brew_install postgresql@16
  log "Starting PostgreSQL"
  brew services start postgresql@16 >/dev/null 2>&1 || brew services restart postgresql@16 >/dev/null 2>&1 || true

  # postgresql@16 is keg-only; make sure its client tools are on PATH.
  local pg_bin
  pg_bin="$(brew --prefix postgresql@16 2>/dev/null)/bin"
  [[ -d "$pg_bin" ]] && export PATH="$pg_bin:$PATH"

  local i=0
  until pg_isready -q >/dev/null 2>&1; do
    ((i += 1))
    ((i >= 30)) && die "PostgreSQL did not accept connections. Run 'brew services list' and check logs."
    sleep 2
  done

  log "Ensuring the mcpkb role and database exist"
  # The default superuser on a Homebrew Postgres install is the current user.
  psql -d postgres -v ON_ERROR_STOP=1 <<'SQL'
DO $$
BEGIN
   IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mcpkb') THEN
      CREATE ROLE mcpkb LOGIN PASSWORD 'mcpkb';
   END IF;
END
$$;
SQL
  if ! psql -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='mcpkb'" | grep -q 1; then
    createdb -O mcpkb mcpkb
  fi

  log "Applying schema from scripts/init_db.sql"
  psql "postgresql://mcpkb:mcpkb@localhost:5432/mcpkb" -v ON_ERROR_STOP=1 -f scripts/init_db.sql >/dev/null
}

# ---------------------------------------------------------------------------
# Neo4j
# ---------------------------------------------------------------------------
start_neo4j() {
  [[ "$SKIP_INSTALL" == "true" ]] || brew_install neo4j
  log "Starting Neo4j"

  # Disable auth for local development to match docker-compose defaults.
  # The Homebrew neo4j formula keeps its config under the keg's libexec/conf.
  local neo4j_conf
  neo4j_conf="$(brew --prefix neo4j 2>/dev/null)/libexec/conf/neo4j.conf"
  if [[ -f "$neo4j_conf" ]]; then
    if grep -qE '^#?dbms\.security\.auth_enabled=' "$neo4j_conf"; then
      # Uncomment / set the existing directive in place.
      sed -i '' -E 's/^#?dbms\.security\.auth_enabled=.*/dbms.security.auth_enabled=false/' "$neo4j_conf"
    else
      printf '\ndbms.security.auth_enabled=false\n' >> "$neo4j_conf"
    fi
    warn "Neo4j authentication disabled for local use in $neo4j_conf"
  else
    warn "Could not locate neo4j.conf under $(brew --prefix neo4j 2>/dev/null)/libexec/conf; ensure auth is disabled manually."
  fi

  brew services start neo4j >/dev/null 2>&1 || brew services restart neo4j >/dev/null 2>&1 || true
  wait_for_http "http://localhost:7474" "Neo4j" 45
  log "Neo4j Browser: http://localhost:7474 (Bolt: bolt://localhost:7687)"
}

# ---------------------------------------------------------------------------
# Qdrant
#
# Qdrant is not published to Homebrew core, so we download the official
# prebuilt macOS binary from GitHub releases into qdrant-bin/.
# ---------------------------------------------------------------------------
QDRANT_VERSION="${QDRANT_VERSION:-v1.19.0}"

install_qdrant() {
  local qdrant_bin="$PROJECT_ROOT/qdrant-bin/qdrant"
  if [[ -x "$qdrant_bin" ]]; then
    log "Qdrant binary already present at $qdrant_bin"
    return
  fi

  local arch asset
  arch="$(uname -m)"
  case "$arch" in
    arm64|aarch64) asset="qdrant-aarch64-apple-darwin.tar.gz" ;;
    x86_64) asset="qdrant-x86_64-apple-darwin.tar.gz" ;;
    *) die "Unsupported CPU architecture for Qdrant: $arch" ;;
  esac

  local url="https://github.com/qdrant/qdrant/releases/download/${QDRANT_VERSION}/${asset}"
  log "Downloading Qdrant ${QDRANT_VERSION} for ${arch}"
  mkdir -p "$PROJECT_ROOT/qdrant-bin"
  local tmp
  tmp="$(mktemp -d)"
  curl --fail --location --silent --show-error "$url" -o "$tmp/qdrant.tar.gz" \
    || die "Could not download Qdrant from $url. Check network access or set QDRANT_VERSION to an available release."
  tar -xzf "$tmp/qdrant.tar.gz" -C "$tmp"
  # The archive contains a single `qdrant` executable.
  local extracted
  extracted="$(find "$tmp" -type f -name qdrant -perm -u+x | head -n 1)"
  [[ -n "$extracted" ]] || extracted="$(find "$tmp" -type f -name qdrant | head -n 1)"
  [[ -n "$extracted" ]] || die "Qdrant archive did not contain a 'qdrant' binary."
  mv "$extracted" "$qdrant_bin"
  chmod +x "$qdrant_bin"
  rm -rf "$tmp"
  log "Qdrant installed at $qdrant_bin"
}

start_qdrant() {
  if check_http "http://localhost:6333/collections" 2>/dev/null; then
    log "Qdrant already running on http://localhost:6333"
    return
  fi
  [[ "$SKIP_INSTALL" == "true" ]] || install_qdrant

  local qdrant_bin="$PROJECT_ROOT/qdrant-bin/qdrant"
  [[ -x "$qdrant_bin" ]] || die "Qdrant binary not found at $qdrant_bin. Rerun without --skip-install."

  # macOS Gatekeeper quarantines downloaded binaries; clear it if present.
  xattr -d com.apple.quarantine "$qdrant_bin" >/dev/null 2>&1 || true

  log "Starting Qdrant in the background"
  mkdir -p data/qdrant
  ( cd data/qdrant && nohup "$qdrant_bin" > "$PROJECT_ROOT/data/logs/qdrant.log" 2>&1 & echo $! > "$PROJECT_ROOT/data/logs/qdrant.pid" )
  wait_for_http "http://localhost:6333/collections" "Qdrant" 30
  log "Qdrant ready: http://localhost:6333"
}

# ---------------------------------------------------------------------------
# Python environment
# ---------------------------------------------------------------------------
VENV_PYTHON=""
setup_python() {
  check_command "$PYTHON_BIN" "Python 3.11+ is required. Install it (brew install python@3.12) and pass --python if needed."
  "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
    || die "Python 3.11+ is required. Current interpreter is too old."

  if [[ ! -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
    log "Creating a Python virtual environment"
    "$PYTHON_BIN" -m venv .venv
  fi
  VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"
  log "Installing the MCP server and dependencies"
  "$VENV_PYTHON" -m pip install --upgrade pip >/dev/null
  "$VENV_PYTHON" -m pip install -e ".[neo4j]" || die "Python dependency installation failed. Check network/proxy, then rerun."
}

configure_env() {
  export GRAPH_BACKEND="neo4j"
  export NEO4J_URI="${NEO4J_URI:-bolt://localhost:7687}"
  export NEO4J_AUTH_ENABLED="${NEO4J_AUTH_ENABLED:-false}"
  export QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"
  export POSTGRES_DSN="${POSTGRES_DSN:-postgresql://mcpkb:mcpkb@localhost:5432/mcpkb}"
  export MCP_KB_TRANSPORT="http"
  export MCP_KB_HTTP_HOST="${MCP_KB_HTTP_HOST:-127.0.0.1}"
  export MCP_KB_HTTP_PORT="${MCP_KB_HTTP_PORT:-7000}"
  export LLM_PROVIDER="${LLM_PROVIDER:-ollama}"
  export LLM_MODEL="${LLM_MODEL:-qwen2.5-coder:14b}"
  export OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-http://localhost:11434/v1}"
  export OLLAMA_GENERATE_URL="${OLLAMA_GENERATE_URL:-http://localhost:11434/api/generate}"
}

verify_neo4j_connection() {
  "$VENV_PYTHON" - <<'PY' || exit 1
from mcp_kb.config import get_settings
from mcp_kb.graph.factory import get_graph_store
try:
    graph = get_graph_store(get_settings())
    print(f"Neo4j ready: {graph.stats()['nodes']} nodes")
except Exception as exc:
    raise SystemExit(f"Neo4j connection failed: {exc}")
PY
}

build_knowledge() {
  [[ "$BUILD_KNOWLEDGE" == "true" ]] || return 0
  local rewrite_args=(-m mcp_kb.ingestion.full_rewrite --skip-clone)
  [[ "$SKIP_OLLAMA_ENRICHMENT" == "true" ]] && rewrite_args+=(--skip-llm-enrichment)
  if [[ "$SKIP_OLLAMA_ENRICHMENT" != "true" ]] && ! check_http "http://localhost:11434/api/tags"; then
    die "Ollama is not reachable at http://localhost:11434. Start Ollama and pull ${LLM_MODEL}, or rerun with --skip-ollama-enrichment."
  fi
  log "Running the full native knowledge rewrite"
  "$VENV_PYTHON" "${rewrite_args[@]}" || die "Native knowledge rewrite failed. Confirm Neo4j, Qdrant, repository access, and Ollama settings."
}

start_mcp_server() {
  if port_open "$MCP_KB_HTTP_HOST" "$MCP_KB_HTTP_PORT"; then
    warn "An MCP HTTP server already responds on port ${MCP_KB_HTTP_PORT}; leaving it running."
    return
  fi
  log "Starting MCP server in the background"
  nohup "$VENV_PYTHON" -m mcp_kb.server > data/logs/mcp-server.log 2>&1 &
  echo $! > data/logs/mcp-server.pid
  local i=0
  until port_open "$MCP_KB_HTTP_HOST" "$MCP_KB_HTTP_PORT"; do
    ((i += 1))
    if ((i >= 15)); then
      warn "MCP server did not open port ${MCP_KB_HTTP_PORT} within 30s. Check data/logs/mcp-server.log."
      return
    fi
    sleep 2
  done
  log "MCP server listening on port ${MCP_KB_HTTP_PORT}"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
ensure_homebrew
start_postgres
start_neo4j
start_qdrant
setup_python
configure_env
verify_neo4j_connection
build_knowledge
start_mcp_server

MCP_URL="http://${MCP_KB_HTTP_HOST}:${MCP_KB_HTTP_PORT}/mcp"

log "Native stack is up."
log "MCP endpoint:   ${MCP_URL}"
log "Neo4j Browser:  http://localhost:7474"
log "Qdrant:         http://localhost:6333"
log "PostgreSQL:     postgresql://mcpkb:mcpkb@localhost:5432/mcpkb"
printf '%s\n' ""
printf '%s\n' "Connect Open WebUI (run separately) to this MCP server:"
printf '%s\n' "Register an MCP streamable HTTP tool server with URL ${MCP_URL}."
printf '%s\n' "See docs/OPENWEBUI_INTEGRATION.md for details."
printf '%s\n' ""
printf '%s\n' "Logs live in data/logs/. Stop background processes with the PIDs in data/logs/*.pid,"
printf '%s\n' "and stop brew services with: brew services stop postgresql@16 neo4j"
