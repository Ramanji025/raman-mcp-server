#!/usr/bin/env bash
# Start the MCP Microservices Knowledge Base on macOS or Linux.
# Docker mode starts Neo4j, Qdrant, PostgreSQL, MCP, and Open WebUI.
# Native mode starts the MCP server against locally running Neo4j/Qdrant/Open WebUI.

set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="docker"
BUILD_KNOWLEDGE="false"
SKIP_OLLAMA_ENRICHMENT="false"
OPEN_WEBUI_PORT="${OPEN_WEBUI_PORT:-3001}"
OPEN_WEBUI_URL="${OPEN_WEBUI_URL:-}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

usage() {
  cat <<'EOF'
Usage:
  ./scripts/start-openwebui-stack.sh [options]

Options:
  --native                    Start only the MCP HTTP server on the host.
                              Neo4j, Qdrant, and Open WebUI must already run locally.
  --build-knowledge           Run the full knowledge rewrite after startup.
  --skip-ollama-enrichment    With --build-knowledge, skip the offline Ollama pass.
  --open-webui-port PORT      Docker Open WebUI host port (default: 3001).
  --open-webui-url URL        Existing Open WebUI URL for native mode.
  --python PATH               Python interpreter for native mode (default: python3).
  --help                      Show this help.

Examples:
  ./scripts/start-openwebui-stack.sh
  ./scripts/start-openwebui-stack.sh --build-knowledge
  ./scripts/start-openwebui-stack.sh --native --open-webui-url http://localhost:3000
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
    --native) MODE="native" ;;
    --build-knowledge) BUILD_KNOWLEDGE="true" ;;
    --skip-ollama-enrichment) SKIP_OLLAMA_ENRICHMENT="true" ;;
    --open-webui-port)
      shift
      (($#)) || die "--open-webui-port requires a port number."
      OPEN_WEBUI_PORT="$1"
      ;;
    --open-webui-url)
      shift
      (($#)) || die "--open-webui-url requires a URL."
      OPEN_WEBUI_URL="$1"
      ;;
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

[[ "$OPEN_WEBUI_PORT" =~ ^[0-9]+$ ]] && ((OPEN_WEBUI_PORT >= 1 && OPEN_WEBUI_PORT <= 65535)) \
  || die "Open WebUI port must be an integer between 1 and 65535."

cd "$PROJECT_ROOT"

check_command() {
  command -v "$1" >/dev/null 2>&1 || die "$2"
}

open_browser() {
  local url="$1"
  if command -v open >/dev/null 2>&1; then
    open "$url" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" >/dev/null 2>&1 || true
  fi
}

check_http() {
  local url="$1"
  curl --fail --silent --show-error --max-time 5 "$url" >/dev/null
}

port_open() {
  local host="$1"
  local port="$2"
  "$PROJECT_ROOT/.venv/bin/python" - "$host" "$port" <<'PY'
import socket
import sys

host, port = sys.argv[1], int(sys.argv[2])
try:
    with socket.create_connection((host, port), timeout=2):
        pass
except OSError:
    raise SystemExit(1)
PY
}

run_docker() {
  check_command docker "Docker is required for Docker mode. Install Docker Desktop on macOS or Docker Engine plus the Compose plugin on Linux."
  docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required. Install the Docker Compose plugin, then run 'docker compose version'."

  export OPEN_WEBUI_PORT="$OPEN_WEBUI_PORT"
  export NEO4J_AUTH="${NEO4J_AUTH:-none}"
  export NEO4J_AUTH_ENABLED="${NEO4J_AUTH_ENABLED:-false}"
  export LLM_PROVIDER="${LLM_PROVIDER:-ollama}"
  export LLM_MODEL="${LLM_MODEL:-qwen2.5-coder:14b}"
  export OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-http://host.docker.internal:11434/v1}"
  export OLLAMA_GENERATE_URL="${OLLAMA_GENERATE_URL:-http://host.docker.internal:11434/api/generate}"

  log "Starting Neo4j, Qdrant, PostgreSQL, MCP server, and Open WebUI in Docker"
  docker compose up -d --build || die "Docker Compose could not start the stack. Run 'docker compose logs --tail=200' and verify ports 3001, 7474, 7687, 8000, 6333, and 5432 are free. Override the WebUI port with --open-webui-port 3002."
  docker compose ps

  local web_ui_url="http://localhost:${OPEN_WEBUI_PORT}"
  log "Open WebUI: ${web_ui_url}"
  log "Neo4j Browser: http://localhost:7474"
  log "MCP endpoint from this host: http://localhost:8000/mcp"
  log "MCP endpoint inside the Docker Open WebUI network: http://mcp-server:8000/mcp"
  printf '%s\n' ""
  printf '%s\n' "One-time Open WebUI registration:"
  printf '%s\n' "1. Sign in at ${web_ui_url}."
  printf '%s\n' "2. Register an MCP tool-server connection of type 'mcp' with URL http://mcp-server:8000/mcp."
  printf '%s\n' "3. If this Open WebUI version does not show an MCP UI, follow docs/OPENWEBUI_INTEGRATION.md."

  if [[ "$BUILD_KNOWLEDGE" == "true" ]]; then
    local rewrite_args=(exec mcp-server python -m mcp_kb.ingestion.full_rewrite --skip-clone --ollama-url http://host.docker.internal:11434)
    [[ "$SKIP_OLLAMA_ENRICHMENT" == "true" ]] && rewrite_args+=(--skip-llm-enrichment)
    if [[ "$SKIP_OLLAMA_ENRICHMENT" != "true" ]] && ! check_http "http://localhost:11434/api/tags"; then
      die "Ollama is not reachable at http://localhost:11434. Start Ollama and pull ${LLM_MODEL}, or rerun with --skip-ollama-enrichment."
    fi
    log "Running the full knowledge rewrite in the MCP container"
    docker compose "${rewrite_args[@]}" || die "Knowledge rewrite failed. Run 'docker compose logs mcp-server' and inspect Neo4j/Qdrant/Ollama connectivity."
  fi

  open_browser "$web_ui_url"
}

run_native() {
  check_command "$PYTHON_BIN" "Python 3.11+ is required for native mode. Install Python and pass --python /path/to/python3 if needed."
  "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
    || die "Python 3.11+ is required. Current interpreter is too old."
  check_command curl "curl is required for native health checks. Install curl using Homebrew, apt, dnf, or your distribution package manager."

  if [[ ! -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
    log "Creating a Python virtual environment"
    "$PYTHON_BIN" -m venv .venv || die "Could not create .venv. Install the Python venv package (for example python3-venv on Debian/Ubuntu)."
  fi
  local venv_python="$PROJECT_ROOT/.venv/bin/python"
  "$venv_python" -m pip install --upgrade pip >/dev/null
  "$venv_python" -m pip install -e ".[neo4j]" || die "Python dependency installation failed. Check Internet/proxy configuration, then rerun."

  check_http "http://localhost:6333/collections" \
    || die "Qdrant is not reachable at http://localhost:6333. Start Qdrant first. A portable option is: docker run -d --name mcpkb-qdrant -p 6333:6333 qdrant/qdrant."

  export GRAPH_BACKEND="neo4j"
  export NEO4J_URI="${NEO4J_URI:-bolt://localhost:7687}"
  export NEO4J_AUTH_ENABLED="${NEO4J_AUTH_ENABLED:-false}"
  export MCP_KB_TRANSPORT="http"
  export MCP_KB_HTTP_HOST="${MCP_KB_HTTP_HOST:-127.0.0.1}"
  export MCP_KB_HTTP_PORT="${MCP_KB_HTTP_PORT:-8000}"
  export LLM_PROVIDER="${LLM_PROVIDER:-ollama}"
  export LLM_MODEL="${LLM_MODEL:-qwen2.5-coder:14b}"
  export OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-http://localhost:11434/v1}"
  export OLLAMA_GENERATE_URL="${OLLAMA_GENERATE_URL:-http://localhost:11434/api/generate}"

  "$venv_python" - <<'PY' || exit 1
from mcp_kb.config import get_settings
from mcp_kb.graph.factory import get_graph_store
try:
    graph = get_graph_store(get_settings())
    print(f"Neo4j ready: {graph.stats()['nodes']} nodes")
except Exception as exc:
    raise SystemExit(f"Neo4j connection failed: {exc}")
PY

  if [[ -n "$OPEN_WEBUI_URL" ]]; then
    check_http "$OPEN_WEBUI_URL" || die "Open WebUI is not reachable at ${OPEN_WEBUI_URL}. Start it first or supply the correct --open-webui-url."
  else
    warn "Native mode does not install Open WebUI. Start an existing Open WebUI instance and rerun with --open-webui-url http://localhost:3000."
  fi

  if [[ "$BUILD_KNOWLEDGE" == "true" ]]; then
    local rewrite_args=(-m mcp_kb.ingestion.full_rewrite --skip-clone)
    [[ "$SKIP_OLLAMA_ENRICHMENT" == "true" ]] && rewrite_args+=(--skip-llm-enrichment)
    if [[ "$SKIP_OLLAMA_ENRICHMENT" != "true" ]] && ! check_http "http://localhost:11434/api/tags"; then
      die "Ollama is not reachable at http://localhost:11434. Start Ollama and pull ${LLM_MODEL}, or rerun with --skip-ollama-enrichment."
    fi
    log "Running the full native knowledge rewrite"
    "$venv_python" "${rewrite_args[@]}" || die "Native knowledge rewrite failed. Confirm Neo4j, Qdrant, repository access, and Ollama settings."
  fi

  mkdir -p data/logs
  if port_open "$MCP_KB_HTTP_HOST" "$MCP_KB_HTTP_PORT"; then
    warn "An MCP HTTP server already responds on port ${MCP_KB_HTTP_PORT}; leaving it running."
  else
    log "Starting MCP server in the background"
    nohup "$venv_python" -m mcp_kb.server > data/logs/mcp-server.log 2>&1 &
    echo $! > data/logs/mcp-server.pid
    sleep 2
    port_open "$MCP_KB_HTTP_HOST" "$MCP_KB_HTTP_PORT" || warn "MCP server did not open port ${MCP_KB_HTTP_PORT}. Check data/logs/mcp-server.log for the startup error."
  fi

  local mcp_url="http://${MCP_KB_HTTP_HOST}:${MCP_KB_HTTP_PORT}/mcp"
  log "Native MCP endpoint: ${mcp_url}"
  if [[ -n "$OPEN_WEBUI_URL" ]]; then
    log "Register ${mcp_url} in Open WebUI as an MCP streamable HTTP server."
    open_browser "$OPEN_WEBUI_URL"
  fi
}

if [[ "$MODE" == "docker" ]]; then
  run_docker
else
  run_native
fi
