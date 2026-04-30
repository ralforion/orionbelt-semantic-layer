# --- Build stage: install dependencies ---
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install dependencies first (cached layer — only reruns when pyproject.toml/uv.lock change)
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --no-dev --no-install-project --frozen

# Copy source and install the project itself
COPY src/ src/
COPY schema/ schema/
COPY osi-obml/osi_obml_converter.py osi-obml/
COPY osi-obml/osi-schema.json osi-obml/
RUN uv sync --no-dev --no-editable --frozen

# --- Runtime stage: minimal image ---
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install IANA tzdata so Python's zoneinfo can resolve names like
# Europe/Berlin. python:3.12-slim has no /usr/share/zoneinfo, and without
# it ZoneInfo("Europe/Berlin") raises ZoneInfoNotFoundError — the timezone
# resolver then silently falls back to UTC. Belt-and-suspenders alongside
# the tzdata Python dep.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -r app && useradd -r -g app -d /app -s /sbin/nologin app

# Copy installed virtualenv from builder
COPY --from=builder --chown=app:app /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Copy schema (needed at runtime for validation)
COPY --from=builder --chown=app:app /app/schema schema/

# Copy OSI converter (needed at runtime for /convert endpoints)
COPY --from=builder --chown=app:app /app/osi-obml osi-obml/

USER app

# Cloud Run injects PORT (default 8080)
ENV PORT=8080 \
    API_SERVER_HOST=0.0.0.0 \
    LOG_LEVEL=INFO \
    LOG_FORMAT=json \
    DISABLE_SESSION_LIST=true \
    EXPOSE_API_DOCS=true \
    EXPOSE_OPENAPI_SCHEMA=true

EXPOSE ${PORT}

# Health check for local Docker / Compose (Cloud Run uses its own probes)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen(f'http://localhost:{__import__(\"os\").environ[\"PORT\"]}/health')"

# Single worker — Cloud Run scales by adding container instances, not workers
CMD ["sh", "-c", "uvicorn orionbelt.api.app:create_app --factory --host 0.0.0.0 --port $PORT --log-level info --proxy-headers --forwarded-allow-ips='*' --no-access-log"]
