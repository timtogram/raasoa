# RAASOA — Knowledge Reliability Layer

**Make enterprise knowledge trustworthy, searchable, and contradiction-free.**

RAASOA sits between your source systems and your AI agents. It doesn't just index documents — it ensures what comes back is accurate, consistent, and governed.

```
  Source Systems          RAASOA                     Consumers
  ─────────────    ─────────────────────    ──────────────────────
  SharePoint  ───►│ Ingest + Quality    │   │ AI Agents (MCP)    │
  Jira        ───►│ Gate + Claim        │──►│ Chat Bots          │
  Confluence  ───►│ Extraction +        │   │ Internal Tools     │
  Notion      ───►│ Contradiction Mgmt  │   │ Search APIs        │
  File Upload ───►│ + Hybrid Retrieval  │   │ Claude / Cursor    │
                  └─────────────────────┘   └──────────────────────┘
```

## What Makes RAASOA Different

Most RAG systems are a vector database with an API. They index everything and hope for the best. RAASOA does four things they don't:

| Capability | What It Does | Why It Matters |
|-----------|-------------|----------------|
| **Quality Visibility** | 7 automated checks produce a quality score (0-1) per document. Low-quality content is quarantined, not silently served. | Your agent won't cite a half-parsed PDF or an empty template page. |
| **Contradiction Management** | LLM extracts factual claims (Subject→Predicate→Value). Conflicting claims across documents are detected automatically. | When Doc A says "Power BI" and Doc B says "SAP", you know — and decide. |
| **Human-in-the-Loop Governance** | Conflicts and quality issues create review tasks. Resolution feeds back into search — superseded docs are excluded. | A human decides which source of truth wins. The system enforces it. |
| **Retrieval You Can Measure** | Built-in evaluation framework with nDCG, Recall, MRR, Answerability. Gold-set based, reproducible. | You can prove your retrieval is better than a naive embedding search. |

## Quickstart (5 Minutes)

```bash
git clone https://github.com/timtogram/raasoa.git
cd raasoa
cp .env.example .env
docker compose up -d
# Wait ~60s for Ollama to pull the embedding model
curl -X POST http://localhost:8000/v1/ingest -F file=@your-document.pdf
```

Open the dashboard at `http://localhost:8000/dashboard` to see quality scores, upload files, and resolve contradictions.

## How It Works

### 1. Ingest → Quality Gate → Claim Extraction

Every document passes through:
1. **Parse** (PDF, DOCX, TXT, MD)
2. **Chunk** (recursive, 512 tokens, 80 overlap)
3. **Embed** (Ollama / OpenAI / Cohere — your choice)
4. **Quality Gate** — 7 checks, score 0-1, auto-quarantine if bad
5. **Claim Extraction** — LLM extracts factual claims as structured triples
6. **Contradiction Detection** — new claims checked against existing knowledge

### 2. Contradiction Detection

```
Doc A: "Our primary visualization tool is Power BI"
  → Claim: (Organization, primary visualization tool, Power BI)

Doc B: "Our central data visualization uses SAP Analytics Cloud"
  → Claim: (Organization, primary visualization tool, SAP Analytics Cloud)

→ Conflict detected (confidence: 89%)
→ Review task created
→ Human decides: "Doc B is current. Doc A is superseded."
→ Doc A excluded from all future search results.
```

### 3. Hybrid Search with Query Routing

Queries are routed to the best strategy:

- **RAG queries** ("How does X work?") → Dense vectors + BM25 + Reciprocal Rank Fusion
- **Structured queries** ("How many documents?") → Direct SQL on metadata
- **ACL-filtered** — only documents the user has access to

### 4. Retrieval Evaluation

```bash
uv run python -m raasoa.eval.runner --gold-set eval/gold_set.json
```

```
RAASOA Retrieval Evaluation Report
============================================================
  Queries evaluated:  7
  Mean nDCG@k:        0.847
  Mean Recall@k:      0.920
  Mean Precision@k:   0.680
  Mean MRR:           0.905
  Answerability:      100%
============================================================
```

## API

```bash
# Ingest a document
curl -X POST /v1/ingest -F file=@doc.pdf -H "Authorization: Bearer sk-key"

# Search (tenant derived from API key, not settable by client)
curl -X POST /v1/retrieve -d '{"query": "...", "top_k": 5}'

# List documents (cursor-paginated)
curl /v1/documents

# Quality report
curl /v1/documents/{id}/quality

# Conflicts
curl /v1/conflicts
curl -X POST /v1/conflicts/{id}/resolve -d '{"resolution": "keep_a"}'

# Analytics
curl /v1/analytics/quality-by-source
curl /v1/analytics/contradiction-hotspots
curl /v1/analytics/claim-stability

# Webhook (for source connectors)
curl -X POST /v1/webhooks/ingest -H "X-Webhook-Secret: whsec-xxx" \
  -d '{"event": "document.created", "source": "notion", ...}'
```

## Dashboard

Upload files, test search, view quality, resolve contradictions — all in the browser.

| Page | What It Shows |
|------|---------------|
| **Overview** | Document count, avg quality, open conflicts, pending reviews |
| **Upload** | Drag & drop file upload with live quality feedback |
| **Search** | Live search playground with routing info and confidence |
| **Sources** | Connected data sources with setup guides |
| **Documents** | All documents with quality scores and conflict status |
| **Conflicts** | Contradictions with inline resolution (Keep A / Keep B / Both Valid) |
| **Reviews** | Review tasks with approve/reject |

## MCP Server (AI Agent Integration)

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

Tools: `raasoa_search`, `raasoa_ingest`, `raasoa_list_documents`, `raasoa_quality_report`, `raasoa_list_conflicts`

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTH_ENABLED` | `true` | Enable API key authentication |
| `API_KEYS` | — | `"key:tenant-uuid"` pairs (comma-separated) |
| `WEBHOOK_SECRET` | — | Shared secret for webhook auth |
| `DASHBOARD_PASSWORD` | — | Dashboard login password |
| `EMBEDDING_PROVIDER` | `ollama` | `ollama`, `openai`, or `cohere` |
| `QUALITY_GATE_ENABLED` | `true` | Enable quality scoring |
| `CLAIM_EXTRACTION_ENABLED` | `true` | Enable LLM claim extraction |
| `CONFLICT_DETECTION_ENABLED` | `true` | Enable contradiction detection |

Full list in `.env.example`.

## Architecture

- **Single database**: PostgreSQL with pgvector + tsvector. No Redis, no Elasticsearch.
- **Model agnostic**: Swap embedding providers with one env variable.
- **Tenant isolation**: Every query is scoped to the authenticated tenant.
- **Content-hash dedup**: Re-ingesting unchanged documents is a no-op.

## Development

```bash
uv sync --extra dev --extra parsing
docker compose up -d postgres
uv run alembic upgrade head
uv run uvicorn raasoa.main:app --reload --port 8000
uv run pytest -v            # 124 tests
uv run ruff check src/      # 0 errors
```

## License

Apache 2.0
