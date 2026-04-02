FROM python:3.12-slim AS base

WORKDIR /app

# System dependencies for PDF/DOCX parsing
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast dependency resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy project files
COPY pyproject.toml uv.lock ./
COPY src/ src/
COPY alembic.ini ./
COPY alembic/ alembic/

# Install dependencies (with parsing extras for PDF/DOCX)
RUN uv sync --frozen --no-dev --extra parsing

# Copy templates for dashboard
COPY src/raasoa/templates/ src/raasoa/templates/

# Entrypoint
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health/ready || exit 1

ENTRYPOINT ["/docker-entrypoint.sh"]
