# RAASOA — Knowledge Reliability Layer

**Trustworthy retrieval with quality gates, contradiction detection, and governance for enterprise knowledge.**

RAASOA sits between your source systems and your AI agents. It doesn't just index documents — it ensures what comes back is accurate, consistent, and governed. No black-box RAG — full control over data quality and knowledge consistency.

```
  Source Systems          RAASOA                     Consumers
  ─────────────    ─────────────────────    ──────────────────────
  SharePoint  ───►│                     │   │ AI Agents (MCP)    │
  Jira        ───►│  Parse + Chunk +    │   │ Chat Bots          │
  Confluence  ───►│  Quality Gate +     │──►│ Internal Apps      │
  Notion      ───►│  Claim Extraction + │   │ Claude / Cursor    │
  File Upload ───►│  3-Layer Retrieval  │   │ REST API Clients   │
  CSV / Excel ───►│                     │   │                    │
                  └─────────────────────┘   └──────────────────────┘
```

## What Makes RAASOA Different

| Capability | What It Does | Why It Matters |
|-----------|-------------|----------------|
| **Quality Visibility** | 7 automated checks produce a quality score (0-1) per document. Low-quality content is quarantined. | Your agent won't cite a half-parsed PDF or an empty template. |
| **Contradiction Management** | LLM extracts factual claims (Subject→Predicate→Value). Conflicting claims across documents are detected automatically. | When Doc A says "Power BI" and Doc B says "SAP", you know — and decide. |
| **Human-in-the-Loop** | Conflicts create review tasks. Resolution feeds back into search — superseded docs are excluded. | A human decides which source of truth wins. The system enforces it. |
| **3-Layer Retrieval** | Knowledge Index (5ms) → Structured SQL (20ms) → Hybrid Search (500ms). Fastest reliable path wins. | Factual queries get instant answers; semantic queries get full RAG. |
| **Knowledge Compilation** | LLM curates and normalizes the knowledge index. Synthesizes topic summaries from claims. | System gets smarter over time — every ingestion improves the index. |
| **Measurable Retrieval** | Built-in eval framework: nDCG, Recall, MRR, Answerability. Gold-set based. | You can prove your retrieval quality with numbers, not guessing. |

## Quickstart

```bash
git clone https://github.com/timtogram/raasoa.git && cd raasoa
cp .env.example .env
docker compose up -d          # PostgreSQL + Ollama + API
# Wait ~90s for Ollama to pull models, then:
curl -X POST http://localhost:8000/v1/ingest -F file=@your-document.pdf
```

Dashboard: `http://localhost:8000/dashboard`

## Supported Formats

| Format | Parsing | Tables | Metadata |
|--------|---------|--------|----------|
| **PDF** | Text + table extraction | Markdown tables | Author, created, subject |
| **DOCX** | Paragraphs + headings + styles | Tables → markdown | Author, title |
| **XLSX** | Multi-sheet, all rows | Per-sheet markdown | Sheet names, row count |
| **PPTX** | Slides + speaker notes | Shape tables | Slide count |
| **CSV/TSV** | Rows as key:value + table | Full markdown table | Headers, row/col count |
| **HTML** | Tag stripping, structure preserved | — | — |
| **TXT/MD** | Direct | — | — |

Tables are rendered as markdown so chunks and claim extraction understand tabular data.

## How It Works

### Ingestion Pipeline

```
File/Webhook → Parse → Chunk → Embed → Quality Gate → Claims → Contradictions → Index
```

1. **Parse** — extract text, tables, metadata from any supported format
2. **Chunk** — recursive splitting, 512 tokens, 80 overlap
3. **Embed** — Ollama (local), OpenAI, Azure OpenAI, Cohere, or custom endpoint
4. **Quality Gate** — 7 checks → score 0-1 → quarantine if bad
5. **Claim Extraction** — LLM extracts factual claims as structured triples
6. **Contradiction Detection** — new claims vs existing knowledge
7. **Knowledge Index** — auto-rebuilt after every ingestion
8. **Data Contract Validation** — webhooks are validated before processing (min length, required fields, blocklist patterns)

### 3-Layer Retrieval with Source Pre-Filtering

Every query passes through three layers — fastest reliable answer wins.
Optionally pre-filter by `source_type` or `doc_type` for targeted search:

```
Query: "What's our primary BI tool?"
  │
  ├─ Layer 1: Knowledge Index    (< 5ms,  100% confidence)
  │  → "SAP Analytics Cloud"     ← direct lookup, no embedding
  │
  ├─ Layer 2: Structured SQL     (< 20ms)
  │  → For "how many documents?" style queries
  │
  └─ Layer 3: Hybrid Search      (200-800ms)
     → Dense + BM25 + Reciprocal Rank Fusion
     → For semantic/conceptual queries
```

All three results come back in one response — the consuming agent picks the best.

### Knowledge Compilation

Inspired by Karpathy's "LLM as knowledge compiler":

- **Claim Extraction** — LLM reads chunks, extracts Subject→Predicate→Value triples with temporal validity (valid_from/valid_until)
- **Knowledge Index** — materialized lookup from normalized claims, rebuilt after every ingestion
- **LLM Curator** — periodically normalizes predicates, merges equivalents, flags inconsistencies
- **Topic Synthesis** — LLM compiles claims per topic into coherent summaries
- **Retrieval Feedback** — search result ratings improve future rankings

The system gets smarter with every document ingested and every query answered.

## API Reference

### Core

```bash
# Ingest (tenant derived from API key)
POST /v1/ingest                              # File upload
POST /v1/webhooks/ingest                     # Webhook (SharePoint, Jira, etc.)

# Retrieve (3-layer: index → structured → hybrid)
POST /v1/retrieve                            # {"query": "...", "top_k": 5}
# Optional: "source_type": "sharepoint", "doc_type": "pdf"
POST /v1/retrieve/feedback                   # Rate results for learning

# Documents
GET  /v1/documents                           # Cursor-paginated list
GET  /v1/documents/{id}                      # Detail with chunks
DELETE /v1/documents/{id}                    # Soft delete
```

### Quality & Governance

```bash
GET  /v1/documents/{id}/quality              # Quality report
GET  /v1/quality/findings                    # All findings
GET  /v1/conflicts                           # Detected contradictions
POST /v1/conflicts/{id}/resolve              # keep_a / keep_b / keep_both
GET  /v1/reviews                             # Review tasks
POST /v1/reviews/{id}/approve                # Approve
POST /v1/reviews/{id}/reject                 # Reject
```

### Knowledge Compilation

```bash
GET  /v1/synthesis                           # List topic summaries
GET  /v1/synthesis/{topic}                   # Get synthesis for topic
POST /v1/synthesis/compile                   # Trigger LLM compilation
POST /v1/synthesis/build-index               # Rebuild knowledge index
POST /v1/synthesis/curate                    # Full LLM curation pipeline
```

### Analytics

```bash
GET  /v1/analytics/quality-by-source         # Quality per data source
GET  /v1/analytics/contradiction-hotspots    # Most unstable knowledge
GET  /v1/analytics/claim-stability           # Claim churn rate
```

### ACL

```bash
POST /v1/acl                                 # Create ACL entry
GET  /v1/acl/{document_id}                   # List ACL entries
DELETE /v1/acl/{entry_id}                    # Delete ACL entry
```

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

**10 Tools:**
`raasoa_search`, `raasoa_ingest`, `raasoa_list_documents`, `raasoa_get_document`, `raasoa_quality_report`, `raasoa_list_conflicts`, `raasoa_feedback`, `raasoa_get_synthesis`, `raasoa_compile`, `raasoa_curate`

## Source Connectors

Connect data sources directly from the dashboard — no code needed:

| Source | Method | Setup |
|--------|--------|-------|
| **Notion** | Native connector | Dashboard → Sources → Enter token → Sync |
| **SharePoint** | Webhook (Power Automate) | Dashboard → Sources → Create → Setup guide |
| **Jira** | Webhook (Automation Rule) | Dashboard → Sources → Create → Setup guide |
| **Confluence** | Webhook (Space Automation) | Dashboard → Sources → Create → Setup guide |
| **Custom** | Webhook (any HTTP) | `POST /v1/webhooks/ingest` |
| **Batch** | CLI | `uv run python -m raasoa.worker ingest /path/` |

```bash
# Source management API
POST /v1/sources                    # Create source with config
GET  /v1/sources                    # List configured sources
POST /v1/sources/{id}/sync          # Trigger sync (Notion: auto-pull)
DELETE /v1/sources/{id}              # Remove source
```

Data contract validation on webhooks: minimum content length, required metadata fields, status filters, content blocklist.

## Embedding Providers

Switch with one environment variable — all local-first, cloud optional:

| Provider | Config | Use Case |
|----------|--------|----------|
| **Ollama** (default) | `EMBEDDING_PROVIDER=ollama` | Local, air-gapped, full control |
| **OpenAI** | `EMBEDDING_PROVIDER=openai` | Cloud, high quality |
| **Azure OpenAI** | `OPENAI_BASE_URL=https://xxx.openai.azure.com/` | Enterprise cloud |
| **Cohere** | `EMBEDDING_PROVIDER=cohere` | Alternative cloud |
| **Custom** | `OPENAI_BASE_URL=http://your-server/v1` | Any OpenAI-compatible endpoint |

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTH_ENABLED` | `true` | API key authentication |
| `API_KEYS` | — | `"key:tenant-uuid"` pairs |
| `WEBHOOK_SECRET` | — | Shared secret for webhooks |
| `DASHBOARD_PASSWORD` | — | Dashboard login password |
| `EMBEDDING_PROVIDER` | `ollama` | `ollama` / `openai` / `cohere` |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Custom/Azure endpoint |
| `QUALITY_GATE_ENABLED` | `true` | Quality scoring |
| `CLAIM_EXTRACTION_ENABLED` | `true` | LLM claim extraction |
| `CONFLICT_DETECTION_ENABLED` | `true` | Contradiction detection |
| `RERANKER` | `passthrough` | `passthrough` / `ollama` |
| `MAX_FILE_SIZE_MB` | `100` | Upload size limit |

Full list in `.env.example`.

## Architecture

- **Single database**: PostgreSQL + pgvector + tsvector. No Redis, no Elasticsearch.
- **Local-first**: Default runs entirely on your machine (Ollama + PostgreSQL).
- **Model agnostic**: Swap providers with one env variable.
- **Tenant isolation**: Every query scoped to the authenticated tenant.
- **Content-hash dedup**: Re-ingesting unchanged documents is a no-op.
- **Auto-curation**: Knowledge index rebuilds after every ingestion. LLM curator normalizes predicates on demand.

## Development

```bash
uv sync --extra dev --extra parsing
docker compose up -d postgres
uv run alembic upgrade head
uv run uvicorn raasoa.main:app --reload --port 8000
uv run pytest -v              # 145+ tests
uv run ruff check src/          # 0 errors
uv run mypy src/raasoa --ignore-missing-imports  # 0 errors
```

## License

Apache 2.0
