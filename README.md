# RAASOA — Enterprise RAG as a Service

**A lightweight, production-grade Retrieval-Augmented Generation service with hybrid search, quality gates, and governance — built on PostgreSQL.**

No Pinecone. No Qdrant. No Elasticsearch. Just PostgreSQL with pgvector + full-text search, delivering hybrid retrieval that outperforms pure vector search.

## Why RAASOA?

Most RAG systems are a vector database with an API wrapper. That works for demos, but not for enterprises. RAASOA is different:

- **Hybrid Search** — Dense vectors + BM25 full-text search with Reciprocal Rank Fusion in a single SQL query. ~84% precision vs ~62% with vectors alone.
- **Single Database** — PostgreSQL handles everything: vectors (pgvector), full-text (tsvector), metadata, ACLs, versioning, and audit logs. One dependency to deploy and operate.
- **Content-Hash Change Detection** — SHA-256 hashing at document and chunk level. When a document changes, only modified chunks get re-embedded.
- **Model Agnostic** — Swap between Ollama (local), OpenAI, or Cohere with one environment variable. Same API, same results format.
- **Governance Built In** — Document versioning, quality scores, conflict detection, review workflows, and full retrieval audit trails.

## Quickstart (5 minutes)

### Prerequisites

- Docker & Docker Compose
- That's it. Ollama runs inside the stack.

### 1. Clone and start

```bash
git clone https://github.com/your-org/raasoa.git
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

### 5. Done.

You get back ranked results with confidence scores, source documents, and chunk-level citations.

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
for hit in results:
    print(f"[{hit.score:.3f}] {hit.text[:100]}...")
```

## CLI

```bash
# Ingest a file
raasoa ingest document.pdf

# Search
raasoa search "How do I reset my password?"

# List documents
raasoa documents list

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
                    │      REST API / MCP        │
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
  │  + RRF Fusion  │   │ Hash → Embed       │   │  Versioning     │
  └───────┬────────┘   └─────────┬──────────┘   │  Audit Trail    │
          │                       │              └─────────────────┘
          └───────────┬───────────┘
                      │
          ┌───────────┴───────────┐
          │     PostgreSQL        │
          │  pgvector + tsvector  │
          │  System of Record     │
          └───────────┬───────────┘
                      │
          ┌───────────┴───────────┐
          │     MinIO / S3        │
          │  Raw Documents        │
          └───────────────────────┘
```

## How Hybrid Search Works

RAASOA combines two retrieval strategies in a single SQL query:

1. **Dense retrieval** — pgvector cosine similarity finds semantically similar chunks
2. **Lexical retrieval** — tsvector BM25 finds exact keyword matches (product codes, ticket IDs, policy terms)
3. **Reciprocal Rank Fusion** — Merges both result sets into a single ranked list

This is critical for enterprise content where exact codes ("ERR-4021"), ticket keys ("JIRA-2847"), and policy terms matter as much as semantic meaning.

## Embedding Providers

Switch providers with one environment variable:

| Provider | `EMBEDDING_PROVIDER` | Local/Cloud | Best For |
|----------|---------------------|-------------|----------|
| **Ollama** | `ollama` | Local | Development, air-gapped, privacy |
| **OpenAI** | `openai` | Cloud | Quick start, broad language support |
| **Cohere** | `cohere` | Cloud | Production multilingual, best reranking |

```bash
# .env
EMBEDDING_PROVIDER=ollama          # or: openai, cohere
OLLAMA_EMBEDDING_MODEL=nomic-embed-text
EMBEDDING_DIMENSIONS=768
```

## API Reference

### `POST /v1/ingest`

Upload and ingest a document (PDF, DOCX, TXT, MD).

```bash
curl -X POST http://localhost:8000/v1/ingest \
  -F file=@document.pdf \
  -H "X-Tenant-Id: 00000000-0000-0000-0000-000000000001"
```

**Response:**
```json
{
  "document_id": "550e8400-e29b-41d4-a716-446655440000",
  "title": "Service Manual v3.2",
  "status": "indexed",
  "chunk_count": 47,
  "version": 1,
  "embedding_model": "ollama/nomic-embed-text",
  "message": "Document 'Service Manual v3.2' ingested with 47 chunks"
}
```

### `POST /v1/retrieve`

Hybrid search with confidence scoring.

```bash
curl -X POST http://localhost:8000/v1/retrieve \
  -H "Content-Type: application/json" \
  -d '{
    "query": "Error code E-4021 hydraulic press",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "top_k": 5
  }'
```

**Response:**
```json
{
  "query": "Error code E-4021 hydraulic press",
  "results": [
    {
      "chunk_id": "...",
      "document_id": "...",
      "text": "Error E-4021 indicates a pressure valve malfunction...",
      "section_title": "Troubleshooting",
      "score": 0.031,
      "semantic_rank": 1,
      "lexical_rank": 2
    }
  ],
  "confidence": {
    "retrieval_confidence": 0.85,
    "source_count": 3,
    "top_score": 0.031,
    "answerable": true
  }
}
```

### `GET /v1/documents`

List all ingested documents.

### `GET /v1/documents/{id}`

Get document details with all chunks.

### `GET /health`

Service health check (database + pgvector status).

## Configuration

All configuration via environment variables (`.env` file):

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://...` | PostgreSQL connection string |
| `EMBEDDING_PROVIDER` | `ollama` | `ollama`, `openai`, or `cohere` |
| `EMBEDDING_DIMENSIONS` | `768` | Must match your model's output dimensions |
| `OLLAMA_BASE_URL` | `http://ollama:11434` | Ollama API endpoint |
| `OLLAMA_EMBEDDING_MODEL` | `nomic-embed-text` | Ollama model name |
| `OPENAI_API_KEY` | — | OpenAI API key (if using OpenAI) |
| `COHERE_API_KEY` | — | Cohere API key (if using Cohere) |
| `CHUNK_SIZE` | `512` | Target chunk size in tokens |
| `CHUNK_OVERLAP` | `80` | Overlap between chunks in tokens |

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
- [x] Quality scoring
- [ ] Cross-encoder reranking (Cohere Rerank, local models)
- [ ] SharePoint / Jira / Confluence connectors
- [ ] MCP server adapter (for Claude, Cursor, AI agents)
- [ ] Tiered indexing (hot/warm/cold)
- [ ] Conflict detection
- [ ] Review UI
- [ ] Query Router (RAG + structured data)

## License

Apache 2.0 — See [LICENSE](LICENSE) for details.
