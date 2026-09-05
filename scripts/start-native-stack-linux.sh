#!/usr/bin/env bash

set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

BUILD_KNOWLEDGE="false"
SKIP_OLLAMA_ENRICHMENT="false"
PYTHON_BIN="${PYTHON_BIN:-python3.11}"

###############################################################################
# Helpers
###############################################################################

log() {
    echo ""
    echo "[setup] $*"
}

warn() {
    echo ""
    echo "[warning] $*" >&2
}

die() {
    echo ""
    echo "[error] $*" >&2
    exit 1
}

check_http() {
    curl -fsS "$1" >/dev/null
}

###############################################################################
# Parse Arguments
###############################################################################

while (($#)); do
    case "$1" in
        --build-knowledge)
            BUILD_KNOWLEDGE=true
            ;;
        --skip-ollama-enrichment)
            SKIP_OLLAMA_ENRICHMENT=true
            ;;
        --python)
            shift
            PYTHON_BIN="$1"
            ;;
        *)
            die "Unknown option: $1"
            ;;
    esac
    shift
done

###############################################################################
# Validate OS
###############################################################################

source /etc/os-release

[[ "$ID" == "amzn" ]] || die "This script requires Amazon Linux 2023"

###############################################################################
# Base Packages
###############################################################################

install_prereqs() {

    log "Installing prerequisites"

    sudo dnf install -y \
        git \
        wget \
        tar \
        unzip \
        gcc \
        gcc-c++ \
        make \
        python3.11 \
        python3.11-pip \
        python3.11-devel \
        java-21-amazon-corretto

    if ! command -v curl >/dev/null 2>&1; then
        die "curl is missing from the system."
    fi
    log "curl version: $(curl --version | head -1)"
}

###############################################################################
# PostgreSQL
###############################################################################

install_postgres() {

    log "Installing PostgreSQL"

    sudo dnf install -y \
        postgresql15 \
        postgresql15-server

    if [[ ! -f /var/lib/pgsql/data/postgresql.conf ]]; then
        sudo postgresql-setup --initdb
    fi

    sudo systemctl enable postgresql
    sudo systemctl restart postgresql

    until pg_isready >/dev/null 2>&1; do
        sleep 2
    done

    log "Creating MCP database"
    # Prevent "could not change directory" errors
    cd /tmp

    # Locate pg_hba.conf
    PG_HBA=$(find /var/lib/pgsql -name pg_hba.conf | head -1)

    log "Updating PostgreSQL authentication: $PG_HBA"

    # Backup
    sudo cp "$PG_HBA" "${PG_HBA}.bak"

    # Change peer/ident to password auth
    sudo sed -i \
    -e 's/^[[:space:]]*local[[:space:]]\+all[[:space:]]\+all[[:space:]]\+peer/local all all md5/' \
    -e 's/^[[:space:]]*local[[:space:]]\+all[[:space:]]\+all[[:space:]]\+ident/local all all md5/' \
    "$PG_HBA"

    # Restart Postgres
    sudo systemctl restart postgresql

    # Wait until ready
    until pg_isready >/dev/null 2>&1; do
        sleep 2
    done

    # Ensure user password exists
    sudo -u postgres psql <<'SQL'
    ALTER USER mcpkb WITH PASSWORD 'mcpkb';
SQL
    sudo -u postgres psql <<'SQL'
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT FROM pg_roles
        WHERE rolname='mcpkb'
    )
    THEN
        CREATE ROLE mcpkb LOGIN PASSWORD 'mcpkb';
    END IF;
END
$$;
SQL

    sudo -u postgres psql -tAc \
    "SELECT 1 FROM pg_database WHERE datname='mcpkb'" \
    | grep -q 1 \
    || sudo -u postgres createdb -O mcpkb mcpkb

    psql \
      postgresql://mcpkb:mcpkb@localhost:5432/mcpkb \
      -f "$PROJECT_ROOT/scripts/init_db.sql"
}

###############################################################################
# Neo4j
###############################################################################

install_neo4j() {

    if command -v neo4j >/dev/null 2>&1; then
        return
    fi

    log "Installing Neo4j"

    sudo rpm --import https://debian.neo4j.com/neotechnology.gpg.key

    cat <<EOF | sudo tee /etc/yum.repos.d/neo4j.repo
[neo4j]
name=Neo4j Repo
baseurl=https://yum.neo4j.com/stable
enabled=1
gpgcheck=0
EOF

    sudo dnf install -y neo4j

    sudo sed -i \
's/^#*dbms.security.auth_enabled=.*/dbms.security.auth_enabled=false/' \
    /etc/neo4j/neo4j.conf

    sudo systemctl enable neo4j
    sudo systemctl restart neo4j

    log "Waiting for Neo4j"

    for i in {1..60}; do
        curl -s http://localhost:7474 >/dev/null && break
        sleep 2
    done

    curl -s http://localhost:7474 >/dev/null \
        || die "Neo4j failed to start"
}

###############################################################################
# Qdrant
###############################################################################

install_qdrant() {

    if systemctl list-unit-files | grep -q qdrant.service; then
        return
    fi

    log "Installing Qdrant"

    VERSION="v1.19.0"

    cd /tmp

    wget \
"https://github.com/qdrant/qdrant/releases/download/${VERSION}/qdrant-x86_64-unknown-linux-gnu.tar.gz"

    tar -xzf qdrant-x86_64-unknown-linux-gnu.tar.gz

    sudo mv qdrant /usr/local/bin/

    sudo useradd -r -s /sbin/nologin qdrant || true

    sudo mkdir -p /var/lib/qdrant

    sudo chown -R qdrant:qdrant \
        /var/lib/qdrant

    cat <<EOF | sudo tee /etc/systemd/system/qdrant.service
[Unit]
Description=Qdrant
After=network.target

[Service]
User=qdrant
Group=qdrant

ExecStart=/usr/local/bin/qdrant

Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable qdrant
    sudo systemctl start qdrant

    for i in {1..30}; do
        curl -s http://localhost:6333/collections >/dev/null && break
        sleep 2
    done

    curl -s \
      http://localhost:6333/collections >/dev/null \
      || die "Qdrant failed"
}

###############################################################################
# Python Environment
###############################################################################

setup_python() {

    cd "$PROJECT_ROOT"

    if [[ ! -d .venv ]]; then

        log "Creating virtual environment"

        "$PYTHON_BIN" -m venv .venv
    fi

    .venv/bin/pip install \
      --upgrade pip

    .venv/bin/pip install \
      -e ".[neo4j]"
}

###############################################################################
# MCP Environment
###############################################################################

configure_env() {

cat > "$PROJECT_ROOT/.env.mcp" <<EOF
GRAPH_BACKEND=neo4j
NEO4J_URI=bolt://localhost:7687
NEO4J_AUTH_ENABLED=false

QDRANT_URL=http://localhost:6333

POSTGRES_DSN=postgresql://mcpkb:mcpkb@localhost:5432/mcpkb

MCP_KB_TRANSPORT=http
MCP_KB_HTTP_HOST=0.0.0.0
MCP_KB_HTTP_PORT=7000

LLM_PROVIDER=ollama
LLM_MODEL=qwen2.5-coder:14b

OLLAMA_BASE_URL=http://localhost:11434/v1
OLLAMA_GENERATE_URL=http://localhost:11434/api/generate
EOF
}

###############################################################################
# Verify Graph
###############################################################################

verify_neo4j() {

log "Validating Neo4j accessibility"

.venv/bin/python <<'PY'
from mcp_kb.config import get_settings
from mcp_kb.graph.factory import get_graph_store

graph = get_graph_store(get_settings())

print(graph.stats())
PY
}

###############################################################################
# MCP Systemd Service
###############################################################################

create_mcp_service() {

log "Creating MCP systemd service"

sudo tee /etc/systemd/system/mcp-kb.service >/dev/null <<EOF
[Unit]
Description=MCP Knowledge Base

After=postgresql.service
After=neo4j.service
After=qdrant.service

Requires=postgresql.service
Requires=neo4j.service
Requires=qdrant.service

[Service]

Type=simple

User=$(whoami)
Group=$(id -gn)

WorkingDirectory=$PROJECT_ROOT
EnvironmentFile=$PROJECT_ROOT/.env.mcp

ExecStart=$PROJECT_ROOT/.venv/bin/python -m mcp_kb.server

Restart=always
RestartSec=10

StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload

sudo systemctl enable mcp-kb
sudo systemctl restart mcp-kb
}

###############################################################################
# Build Knowledge
###############################################################################

build_knowledge() {

    [[ "$BUILD_KNOWLEDGE" == "true" ]] || return

    CMD=(
      .venv/bin/python
      -m
      mcp_kb.ingestion.full_rewrite
      --skip-clone
    )

    if [[ "$SKIP_OLLAMA_ENRICHMENT" == "true" ]]; then
        CMD+=(--skip-llm-enrichment)
    fi

    "${CMD[@]}"
}

###############################################################################
# Main
###############################################################################

install_prereqs
install_postgres
install_neo4j
install_qdrant

setup_python
configure_env
verify_neo4j

build_knowledge

create_mcp_service

echo ""
echo "========================================"
echo "MCP STACK READY"
echo "========================================"
echo ""

echo "MCP Endpoint"
echo "http://localhost:7000/mcp"
echo ""

echo "Neo4j"
echo "http://localhost:7474"
echo ""

echo "Qdrant"
echo "http://localhost:6333"
echo ""

echo "Postgres"
echo "postgresql://mcpkb:mcpkb@localhost:5432/mcpkb"
echo ""

echo "Systemd Status"
echo "sudo systemctl status mcp-kb"
echo ""

echo "Live Logs"
echo "sudo journalctl -u mcp-kb -f"
echo ""

echo "Restart MCP"
echo "sudo systemctl restart mcp-kb"