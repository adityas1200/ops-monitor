# ---- Stage 1: build React UI -----------------------------------------------
FROM node:20-alpine AS frontend-build
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- Stage 2: FastAPI + static UI ------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8001 \
    OPS_MONITOR_RELOAD=0

WORKDIR /app

# System deps used by snowflake-connector (and common SSL/crypto wheels)
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      gcc \
      libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend/ /app/backend/
COPY --from=frontend-build /build/dist /app/frontend/dist

# Writable dirs for runtime settings + agent memory (ephemeral on Fargate;
# mount EFS over these paths if you need persistence across tasks)
RUN mkdir -p /app/backend/app/data /app/backend/app/memory/store \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app

USER appuser
WORKDIR /app/backend

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/api/health" || exit 1

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
