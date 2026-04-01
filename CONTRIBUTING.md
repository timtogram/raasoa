# Contributing to RAASOA

Thank you for your interest in contributing to RAASOA!

## Development Setup

```bash
# Clone the repo
git clone https://github.com/your-org/raasoa.git
cd raasoa

# Install uv (if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install all dependencies
uv sync --extra dev --extra parsing

# Start infrastructure
docker compose up -d postgres minio ollama

# Run database migrations
uv run alembic upgrade head

# Start the dev server
uv run uvicorn raasoa.main:app --reload --port 8000

# Run tests
uv run pytest -v

# Lint
uv run ruff check src/ tests/
```

## Project Structure

```
src/raasoa/
├── api/          # FastAPI route handlers
├── ingestion/    # Document parsing, chunking, embedding pipeline
├── models/       # SQLAlchemy ORM models
├── providers/    # Embedding & reranking provider interfaces
├── retrieval/    # Hybrid search, reranking, confidence scoring
├── schemas/      # Pydantic request/response models
├── config.py     # Application settings
├── db.py         # Database engine and session
└── main.py       # FastAPI app entry point
```

## Guidelines

- **Tests required** — All new features need tests. Run `uv run pytest -v` before submitting.
- **Linting** — Run `uv run ruff check src/ tests/` and fix all issues.
- **Type hints** — All functions must have type annotations.
- **Conventional commits** — Use `feat:`, `fix:`, `docs:`, `test:`, `refactor:` prefixes.
- **Small PRs** — One feature or fix per PR. Easier to review, faster to merge.

## Architecture Decisions

Major architectural decisions are documented in `docs/rag-service-konzept.md`. Key principles:

1. **PostgreSQL as single database** — No separate vector DB. pgvector + tsvector in one system.
2. **Hybrid search** — Dense + BM25 + RRF fusion. Never pure vector search.
3. **Content-hash change detection** — SHA-256 at document and chunk level.
4. **Provider-agnostic** — Embedding and reranking via Protocol interfaces.
5. **Governance built in** — Versioning, quality gates, audit trails from day one.
