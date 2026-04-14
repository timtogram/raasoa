# Contributing to RAASOA

## Development Setup

```bash
git clone https://github.com/timtogram/raasoa.git
cd raasoa

# Install uv (if needed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install all dependencies
uv sync --extra dev --extra parsing

# Start infrastructure
docker compose up -d postgres

# Run database migrations
uv run alembic upgrade head

# Start the dev server
uv run uvicorn raasoa.main:app --reload --port 8000

# Quality checks (all three must pass)
uv run pytest -v           # 145 tests
uv run ruff check src/     # 0 errors
uv run mypy src/raasoa     # 0 errors
```

## Project Structure

```
src/raasoa/
├── api/            # FastAPI route handlers
│   ├── ingestion.py    # POST /v1/ingest
│   ├── retrieval.py    # POST /v1/retrieve (3-layer)
│   ├── documents.py    # CRUD with cursor pagination
│   ├── quality.py      # Quality, conflicts, reviews
│   ├── acl.py          # Access control lists
│   ├── analytics.py    # Quality-by-source, hotspots, stability
│   ├── synthesis.py    # Knowledge compilation + curator
│   ├── webhooks.py     # External source connectors
│   └── health.py       # Health + readiness probes
├── connectors/     # Source-specific connectors (Notion, etc.)
├── dashboard/      # HTMX + Jinja2 governance UI
├── eval/           # Retrieval evaluation framework (nDCG, Recall, MRR)
├── ingestion/      # Parse → Chunk → Embed → Quality → Claims
│   ├── parser.py       # PDF, DOCX, XLSX, PPTX, CSV, HTML, TXT
│   ├── chunker.py      # Recursive token-based splitting
│   ├── pipeline.py     # Full ingestion orchestrator
│   ├── validation.py   # Data contract validation
│   └── hasher.py       # SHA-256 content hashing
├── mcp/            # MCP server (10 tools for AI agents)
├── middleware/      # Auth (API key → tenant), rate limiting
├── models/         # SQLAlchemy ORM (documents, chunks, claims, etc.)
├── providers/      # Embedding providers (Ollama, OpenAI, Cohere, custom)
├── quality/        # Quality gates, claims, conflicts, synthesis, curator
│   ├── checks.py       # 7 rule-based quality checks
│   ├── claims.py       # LLM claim extraction (parallel, multi-pass)
│   ├── conflicts.py    # 4-pass conflict detection
│   ├── claim_conflicts.py  # Claim-to-claim contradiction
│   ├── judge.py        # LLM-as-Judge for auto-resolving conflicts
│   ├── synthesis.py    # Topic summary compilation
│   └── curator.py      # LLM-powered index normalization + lint
├── retrieval/      # 3-layer retrieval
│   ├── knowledge_index.py  # Layer 1: materialized claim lookup (<5ms)
│   ├── structured.py       # Layer 2: SQL metadata queries (<20ms)
│   ├── hybrid_search.py    # Layer 3: Dense + BM25 + RRF + Feedback Boost
│   ├── feedback.py         # Cumulative retrieval learning
│   └── reranker.py         # Passthrough / Ollama / Cohere
├── providers/      # Embedding providers + cache
│   ├── cache.py        # LRU embedding cache (saves 30-50% API costs)
│   ├── factory.py      # Provider selection + cache wrapping
│   ├── ollama.py       # Ollama (local, default)
│   ├── openai.py       # OpenAI / Azure / custom endpoint
│   └── cohere.py       # Cohere
├── schemas/        # Pydantic request/response models
├── templates/      # Dashboard HTML (Tailwind + HTMX)
└── worker/         # Background tasks
    ├── batch.py        # Batch ingestion + maintenance
    ├── queue.py        # PostgreSQL-based job queue
    ├── retention.py    # GDPR hard-delete cleanup
    └── sync_scheduler.py  # Scheduled source sync
```

## Guidelines

- **All three checks must pass** before commit: `pytest`, `ruff`, `mypy`
- **Type hints** — all functions with explicit types. `mypy --strict` enforced.
- **Conventional commits** — `feat:`, `fix:`, `docs:`, `test:`, `refactor:`
- **Small PRs** — one feature or fix per PR.
- **No `any` types** — use explicit types or `dict[str, Any]`.

## Architecture Principles

1. **PostgreSQL only** — pgvector + tsvector + pg_trgm, no separate vector DB.
2. **3-layer retrieval** — Knowledge Index → Structured SQL → Hybrid Search.
3. **Claims as knowledge atoms** — LLM extracts Subject→Predicate→Value triples.
4. **LLM Judge** — AI resolves high-confidence conflicts, humans handle the rest.
5. **Auto-curation** — Knowledge index rebuilds after every ingestion.
6. **Local-first** — Ollama default, cloud optional. One ENV to switch.
7. **API/MCP-first** — dashboard is governance UI, not end-user product.
8. **Multi-tenant** — tenant-isolated from the start.
