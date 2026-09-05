FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Build tools needed for tree-sitter, lxml, psycopg
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git libxml2-dev libxslt1-dev libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt pyproject.toml ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY src ./src
COPY config ./config
COPY scripts ./scripts
RUN pip install -e ".[neo4j]"

# Pre-warm the local embedding model so first request is fast.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')" || true

EXPOSE 8000

CMD ["python", "-m", "mcp_kb.server"]
