# RAASOA — Enterprise RAG as a Service

**A lightweight, production-grade Retrieval-Augmented Generation service with hybrid search, quality gates, and governance — built on PostgreSQL.**

No Pinecone. No Qdrant. No Elasticsearch. Just PostgreSQL with pgvector + full-text search, delivering hybrid retrieval that outperforms pure vector search.

## Why RAASOA?

Most RAG systems are a vector database with an API wrapper. That works for demos, but not for enterprises. RAASOA is different:

- **Hybrid Search** — Dense vectors + BM25 full-text search with Reciprocal Rank Fusion in a single SQL query. ~84% precision vs ~62% with vectors alone.
- **Single Database** — PostgreSQL handles everything: vectors (pgvector), full-text (tsvector), metadata, ACLs, versioning, and audit logs. One dependency to deploy and operate.
- **Content-Hash Change Detection** — SHA-256 hashing at document and chunk level. When a document changes, only modified chunks get re-embedded.
- **Quality Gates** — 7 rule-based checks producing a quality score (0.0-1.0). Automatic quarantine, review workflows, and conflict detection.
- **Claim-based Contradiction Detection** — LLM extracts factual claims as Subject-Predicate-Object triples. Contradictions between documents are detected automatically.
- **Model Agnostic** — Swap between Ollama (local), OpenAI, or Cohere with one environment variable. Same API, same results format.
- **Governance Built In** — Document versioning, quality scores, conflict detection, review workflows, ACLs, and full retrieval audit trails.

## Quickstart (5 minutes)

### Prerequisites

- Docker & Docker Compose
- That's it. Ollama runs inside the stack.

### 1. Clone and start

```bash
git clone https://github.com/timtogram/raasoa.git
cd raasoa
cp .env.example .env
docker compose up -d
```

This starts PostgreSQL (with pgvector), MinIO (object storage), and Ollama (local embeddings).

### 2. Wait for Ollama to pull the embedding model (~1 min first time)

```bash
docker compose logs -f ollama
# Wait for "model pulled successfully"
```

### 3. Ingest a document

```bash
curl -X POST http://localhost:8000/v1/ingest \
  -F file=@your-document.pdf
```

### 4. Search

```bash
curl -X POST http://localhost:8000/v1/retrieve \
  -H "Content-Type: application/json" \
  -d '{
    "query": "How does the authentication system work?",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "top_k": 5
  }'
```

### 5. Open the Dashboard

```
http://localhost:8000/dashboard
```

View documents, quality scores, conflicts, and resolve review tasks — all in your browser.

## Python Client

```bash
pip install raasoa-client
```

```python
from raasoa_client import RAGClient

client = RAGClient("http://localhost:8000")

# Ingest
doc = client.ingest("path/to/document.pdf")
print(f"Ingested: {doc.title} ({doc.chunk_count} chunks)")

# Search
results = client.search("What is the refund policy?")
for hit in results.results:
    print(f"[{hit.score:.3f}] {hit.text[:100]}...")

# Quality & governance
report = client.quality_report(str(doc.document_id))
conflicts = client.conflicts()
reviews = client.reviews()

# Resolve a conflict
client.resolve_conflict(conflict_id, "keep_a", comment="Doc A is more recent")

# Soft-delete a document
client.delete_document(str(doc.document_id))
```

## CLI

```bash
# Ingest a file
raasoa ingest document.pdf

# Search (with query routing)
raasoa search "How do I reset my password?"

# List documents (cursor-paginated)
raasoa documents --limit 20

# Quality report
raasoa quality <document-id>

# List conflicts and resolve
raasoa conflicts --status new
raasoa resolve <conflict-id> keep_a --comment "Doc A is canonical"

# Review workflows
raasoa reviews --status new
raasoa approve <review-id>

# Soft-delete
raasoa delete <document-id>

# Check service health
raasoa health
```

## Architecture

```
                         ┌─────────────────┐
                         │   Your App /     │
                         │   Bot / Agent    │
                         └────────┬─────────┘
                                  │
                    ┌─────────────┴─────────────┐
                    │      REST API / CLI        │
                    │    (FastAPI + uvicorn)      │
                    └─────────────┬──────────────┘
                                  │
                    ┌─────────────┴──────────────┐
                    │       Query Router          │
                    │  RAG ←→ Structured Query    │
                    └─────────────┬──────────────┘
                                  │
          ┌───────────────────────┼───────────────────────┐
          │                       │                       │
  ┌───────┴────────┐   ┌─────────┴─────────┐   ┌────────┴────────┐
  │  Hybrid Search │   │ Ingestion Pipeline │   │  Governance     │
  │  Dense + BM25  │   │ Parse → Chunk →    │   │  Quality Gates  │
  │  + RRF Fusion  │   │ Hash → Embed →     │   │  Claim Extract  │
  │  + Reranking   │   │ Quality → Claims   │   │  Conflict Det.  │
  └───────┬────────┘   └─────────┬──────────┘   │  ACLs + Audit   │
          │                       │              └─────────────────┘
          └───────────┬───────────┘
                      │
          ┌───────────┴───────────┐
          │     PostgreSQL        │
          │  pgvector + tsvector  │
          │  System of Record     │
          └───────────────────────┘
```

## How Hybrid Search Works

RAASOA combines two retrieval strategies in a single SQL query:

1. **Dense retrieval** — pgvector cosine similarity finds semantically similar chunks
2. **Lexical retrieval** — tsvector BM25 finds exact keyword matches (product codes, ticket IDs, policy terms)
3. **Reciprocal Rank Fusion** — Merges both result sets into a single ranked list
4. **Reranking** — Optional LLM-based or cross-encoder reranking for precision

This is critical for enterprise content where exact codes ("ERR-4021"), ticket keys ("JIRA-2847"), and policy terms matter as much as semantic meaning.

## Quality Gates & Conflict Detection

Every ingested document passes through 7 quality checks:

| Check | What it Detects | Severity |
|-------|----------------|----------|
| Empty content | Parser extracted no text | Critical |
| Short content | Document too short for meaningful RAG | Warning |
| No title | Missing document title | Warning |
| High boilerplate | Too much repeated/template text | Warning |
| Embedding failures | Chunks that failed embedding | Warning |
| Tiny chunks | Too many very small chunks | Info |
| No chunks from content | Content exists but chunker produced nothing | Warning |

### Claim-based Contradiction Detection

After quality checks, RAASOA extracts factual claims using an LLM:

```
Document A: "Our primary visualization tool is Power BI"
  → Claim: (Organization, primary visualization tool, Power BI)

Document B: "Our central data visualization uses SAP"
  → Claim: (Organization, primary visualization tool, SAP)

→ Conflict detected: Same predicate, different values
```

Conflicts are surfaced in the dashboard and API for human review. Resolution feeds back into search — superseded documents are excluded from retrieval results.

## Query Router

RAASOA automatically routes queries to the optimal strategy:

| Query Type | Example | Strategy |
|-----------|---------|----------|
| Knowledge | "How does authentication work?" | RAG (hybrid search) |
| Aggregation | "How many documents do we have?" | Structured (SQL) |
| Factual | "What is the average quality score?" | Structured (SQL) |
| Ambiguous | "Power BI visualization" | RAG (default fallback) |

## Tiered Indexing

Not all documents need full vector embeddings:

| Tier | Indexing | Search | Use Case |
|------|---------|--------|----------|
| **Hot** | Full embeddings | Dense + BM25 | Active, frequently accessed docs |
| **Warm** | Summary embedding | Summary-level dense + BM25 | Lower-priority docs |
| **Cold** | BM25 only | Lexical only | Archival, rarely accessed docs |

Documents are automatically promoted/demoted based on access patterns and quality scores.

## Embedding Providers

Switch providers with one environment variable:

| Provider | `EMBEDDING_PROVIDER` | Local/Cloud | Best For |
|----------|---------------------|-------------|----------|
| **Ollama** | `ollama` | Local | Development, air-gapped, privacy |
| **OpenAI** | `openai` | Cloud | Quick start, broad language support |
| **Cohere** | `cohere` | Cloud | Production multilingual, best reranking |

## API Reference

### Ingestion

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v1/ingest` | POST | Upload and ingest a document (PDF, DOCX, TXT, MD) |

### Retrieval

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v1/retrieve` | POST | Hybrid search with query routing and confidence scoring |

### Documents

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v1/documents` | GET | List documents (cursor-paginated) |
| `/v1/documents/{id}` | GET | Document details with all chunks |
| `/v1/documents/{id}` | DELETE | Soft-delete a document |

### Quality & Governance

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v1/documents/{id}/quality` | GET | Quality report with findings |
| `/v1/quality/findings` | GET | List quality findings across all documents |
| `/v1/conflicts` | GET | List conflict candidates |
| `/v1/conflicts/{id}/resolve` | POST | Resolve a conflict (keep_a/keep_b/keep_both/reject_both) |
| `/v1/reviews` | GET | List review tasks |
| `/v1/reviews/{id}/approve` | POST | Approve a review |
| `/v1/reviews/{id}/reject` | POST | Reject a review |

### Access Control

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v1/acl` | POST | Create an ACL entry for a document |
| `/v1/acl/{document_id}` | GET | List ACL entries |
| `/v1/acl/{entry_id}` | DELETE | Remove an ACL entry |

### Webhooks (Source Connectors)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v1/webhooks/ingest` | POST | Receive document events from external sources |

Supports events: `document.created`, `document.updated`, `document.deleted` from any source (SharePoint, Jira, Confluence, custom).

### Health

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Full health check (DB, pgvector, embedding, LLM) |
| `/health/ready` | GET | Lightweight readiness probe for load balancers |

### Dashboard

| Endpoint | Description |
|----------|-------------|
| `/dashboard` | Overview with stats |
| `/dashboard/documents` | Document list with quality scores |
| `/dashboard/documents/{id}` | Document detail with claims and findings |
| `/dashboard/conflicts` | Conflict list with inline resolution |
| `/dashboard/reviews` | Review tasks with approve/reject |

## MCP Server (AI Agent Integration)

RAASOA includes a built-in MCP (Model Context Protocol) server, allowing AI agents like Claude Desktop, Cursor, and Windsurf to directly search and manage your knowledge base.

### Setup for Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "raasoa": {
      "command": "uv",
      "args": ["run", "python", "-m", "raasoa.mcp"],
      "cwd": "/path/to/raasoa"
    }
  }
}
```

### Available MCP Tools

| Tool | Description |
|------|-------------|
| `raasoa_search` | Hybrid search with confidence scoring |
| `raasoa_ingest` | Ingest text content into the knowledge base |
| `raasoa_list_documents` | List all documents with quality info |
| `raasoa_get_document` | Get full document details and chunks |
| `raasoa_quality_report` | Quality report with findings |
| `raasoa_list_conflicts` | List detected contradictions |

## Background Worker

Batch operations without Celery or Redis:

```bash
# Batch ingest all files from a directory
uv run python -m raasoa.worker ingest /path/to/documents/

# Run maintenance (tiering sweep, cleanup)
uv run python -m raasoa.worker maintenance

# Tiering sweep only
uv run python -m raasoa.worker tiering
```

## Configuration

All configuration via environment variables (`.env` file):

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://...` | PostgreSQL connection string |
| `EMBEDDING_PROVIDER` | `ollama` | `ollama`, `openai`, or `cohere` |
| `EMBEDDING_DIMENSIONS` | `768` | Must match your model's output dimensions |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama API endpoint |
| `OLLAMA_EMBEDDING_MODEL` | `nomic-embed-text` | Ollama embedding model |
| `OLLAMA_CHAT_MODEL` | `qwen3:8b` | LLM for claim extraction |
| `CHUNK_SIZE` | `512` | Target chunk size in tokens |
| `CHUNK_OVERLAP` | `80` | Overlap between chunks in tokens |
| `MAX_FILE_SIZE_MB` | `100` | Maximum upload file size |
| `QUALITY_GATE_ENABLED` | `true` | Enable/disable quality gates |
| `QUALITY_PUBLISH_THRESHOLD` | `0.8` | Min score for auto-publish |
| `CONFLICT_DETECTION_ENABLED` | `true` | Enable conflict detection |
| `CLAIM_EXTRACTION_ENABLED` | `true` | Enable LLM claim extraction |
| `RERANKER` | `passthrough` | `passthrough` or `ollama` |
| `INGEST_RATE_LIMIT_PER_MINUTE` | `30` | Rate limit for ingestion |
| `RETRIEVE_RATE_LIMIT_PER_MINUTE` | `120` | Rate limit for retrieval |
| `DASHBOARD_ENABLED` | `true` | Enable governance dashboard |

## Development

```bash
# Install dependencies
uv sync --extra dev --extra parsing

# Start infrastructure
docker compose up -d postgres minio

# Run migrations
uv run alembic upgrade head

# Start dev server
uv run uvicorn raasoa.main:app --reload --port 8000

# Run tests
uv run pytest -v

# Lint
uv run ruff check src/ tests/
```

## Roadmap

- [x] Hybrid Search (Dense + BM25 + RRF)
- [x] Multi-provider embeddings (Ollama, OpenAI, Cohere)
- [x] Content-hash change detection
- [x] Document versioning
- [x] Quality scoring (7 rule-based checks)
- [x] Claim-based contradiction detection (LLM-powered)
- [x] Conflict resolution workflow
- [x] HTMX + Jinja2 governance dashboard
- [x] Query Router (RAG + structured queries)
- [x] Tiered indexing (hot/warm/cold)
- [x] ACL enforcement (per-document access control)
- [x] Retrieval audit trail
- [x] Cursor-based pagination
- [x] Rate limiting (per-tenant)
- [x] Cross-encoder / LLM reranking (Ollama)
- [x] Python client SDK + CLI (12 commands)
- [x] Docker with health checks and DB wait loop
- [x] MCP server adapter (for Claude Desktop, Cursor, AI agents)
- [x] Webhook-based source connectors (SharePoint, Jira, Confluence, custom)
- [x] Background worker for batch ingestion + maintenance
- [x] OpenTelemetry tracing (optional)
- [ ] Native SharePoint / Jira / Confluence polling connectors

## License

Apache 2.0 — See [LICENSE](LICENSE) for details.
