FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TRILIUM_BACKUP_DIR=/tmp/trilium-mcp-backups

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

RUN useradd -m mcp && mkdir -p /tmp/trilium-mcp-backups && chown mcp /tmp/trilium-mcp-backups
USER mcp

# stdio MCP server: run with `docker run --rm -i`
ENTRYPOINT ["trilium-mcp"]
