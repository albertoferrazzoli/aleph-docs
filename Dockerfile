# Multi-stage Dockerfile for aleph-docs.
#
# Stages:
#   - frontend-build : Node 20 builds aleph/frontend/ into a static dist.
#   - base           : Python 3.12 + ffmpeg + git (shared by mcp + aleph).
#   - mcp            : the MCP server, exposed on :8001.
#   - aleph          : the Aleph FastAPI backend + bundled static frontend,
#                       exposed on :8765.
#
# Build both images via `docker compose build`. See docker-compose.yml
# for the runtime wiring.

# ----------------------------------------------------------------------
# Stage 1 — build the Vite frontend into a static bundle.
# ----------------------------------------------------------------------
FROM node:20-alpine AS frontend-build
WORKDIR /fe
COPY aleph/frontend/package.json aleph/frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY aleph/frontend/ ./
RUN npm run build

# ----------------------------------------------------------------------
# Stage 2 — Python 3.12 runtime base with ffmpeg + build deps for
# pgvector, pypdfium2, pillow, psycopg, etc.
# ----------------------------------------------------------------------
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ffmpeg git ca-certificates curl \
        build-essential python3-dev \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app

# ----------------------------------------------------------------------
# Stage 3 — MCP server.
# ----------------------------------------------------------------------
FROM base AS mcp
COPY mcp/requirements.txt /app/mcp/requirements.txt
RUN pip install -r /app/mcp/requirements.txt
COPY mcp/ /app/mcp/
WORKDIR /app/mcp
# Where local docs are mounted (docker-compose.yml binds ./docs → /docs).
ENV LOCAL_DOCS_PATH=/docs \
    DOCS_DB_PATH=/data/index.db \
    DOCS_REPO_PATH=/data/repo \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8001
EXPOSE 8001
CMD ["python", "server.py"]

# ----------------------------------------------------------------------
# Stage 4 — Aleph backend + bundled frontend.
# The frontend dist from stage 1 is copied into /app/aleph/frontend/dist.
# A small entrypoint wraps the existing FastAPI app and adds a static
# mount so ONE uvicorn process serves both /aleph/api/* (API) and
# /aleph/* (static files) on the same port.
# ----------------------------------------------------------------------
FROM base AS aleph
# MCP package is needed by aleph.backend (imports `from memory import ...`).
COPY mcp/requirements.txt /app/mcp/requirements.txt
COPY aleph/backend/requirements.txt /app/aleph/backend/requirements.txt
RUN pip install -r /app/mcp/requirements.txt -r /app/aleph/backend/requirements.txt
COPY mcp/ /app/mcp/
COPY aleph/backend/ /app/aleph/backend/
COPY --from=frontend-build /fe/dist /app/aleph/frontend/dist
COPY docker/entrypoint-aleph.py /app/entrypoint-aleph.py
ENV PYTHONPATH=/app/mcp:/app/aleph/backend \
    MCP_PATH=/app/mcp \
    ALEPH_HOST=0.0.0.0 \
    ALEPH_PORT=8765 \
    ALEPH_FRONTEND_DIST=/app/aleph/frontend/dist
WORKDIR /app
EXPOSE 8765
CMD ["python", "entrypoint-aleph.py"]
