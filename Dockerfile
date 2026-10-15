FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    IFWI_MCP_SERVER_MODE=server \
    IFWI_MCP_SERVER_HOST=0.0.0.0 \
    IFWI_MCP_SERVER_PORT=8000 \
    IFWI_MCP_EXEC_MODE=remote \
    IFWI_MCP_CACHE_DIR=/data/cache

WORKDIR /app

# Install only the server package. Credentials and configuration are supplied at runtime.
COPY pyproject.toml README.md ./
COPY ifwi_mcp/ ./ifwi_mcp/
RUN python -m pip install --no-cache-dir . \
    && groupadd --system ifwi \
    && useradd --system --gid ifwi --home-dir /data --no-create-home ifwi \
    && mkdir -p /data/cache \
    && chown -R ifwi:ifwi /data

USER ifwi
EXPOSE 8000
CMD ["python", "-m", "ifwi_mcp.server"]