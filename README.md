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
  CSV / Excel ───►│  + LLM Judge        │   │                    │
                  └─────────────────────┘   └──────────────────────┘
```

## What Makes RAASOA Different

| Capability | What It Does | Why It Matters |
|-----------|-------------|----------------|
| **Quality Visibility** | 7 automated checks produce a quality score (0-1) per document. Low-quality content is quarantined. | Your agent won't cite a half-parsed PDF or an empty template. |
| **Contradiction Management** | LLM extracts factual claims (Subject→Predicate→Value). Conflicting claims across documents are detected automatically. | When Doc A says "Power BI" and Doc B says "SAP", you know — and decide. |
| **LLM Judge** | AI evaluates conflicts and auto-resolves high-confidence ones. Configurable threshold — humans handle the rest. | 80% of conflicts resolved automatically. Humans focus on the hard cases. |
| **3-Layer Retrieval** | Knowledge Index (5ms) → Structured SQL (20ms) → Hybrid Search (500ms). Fastest reliable path wins. | Factual queries get instant answers; semantic queries get full RAG. |
| **Knowledge Compilation** | LLM curates the knowledge index, normalizes predicates, synthesizes topics. Multi-pass extraction. | System gets smarter over time — every ingestion improves the index. |
| **Measurable Retrieval** | Built-in eval: nDCG, Recall, MRR, Answerability. Embedding cache saves 30-50% of API costs. | Prove your retrieval quality with numbers. |

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

| Format | Parsing | Tables | Metadata | Page Tracking |
|--------|---------|--------|----------|---------------|
| **PDF** | Text + table extraction | Markdown tables | Author, created, subject | Page number |
| **DOCX** | Paragraphs + headings + styles | Tables → markdown | Author, title | — |
| **XLSX** | Multi-sheet, all rows | Per-sheet markdown | Sheet names, row count | Sheet name |
| **PPTX** | Slides + speaker notes | Shape tables | Slide count | Slide number |
| **CSV/TSV** | Rows as key:value + table | Full markdown table | Headers, row/col count | — |
| **HTML** | Tag stripping, structure preserved | — | — | — |
| **TXT/MD** | Direct | — | — | — |

Every search result includes the source location: "Page 5", "Slide 3", "Sheet: Revenue".

## How It Works

### Ingestion Pipeline

```
File/Webhook → Parse → Chunk → Embed → Quality Gate → Claims → Contradictions → LLM Judge → Index
```

1. **Parse** — extract text, tables, metadata from any supported format
2. **Chunk** — recursive splitting, 512 tokens, 80 overlap, page tracking
3. **Embed** — via Embedding Cache (dedup identical texts, saves 30-50% API costs)
4. **Quality Gate** — 7 checks → score 0-1 → quarantine if bad
5. **Claim Extraction** — LLM extracts factual claims with temporal validity. Multi-pass optional (+15-25% more claims)
6. **Contradiction Detection** — new claims vs existing knowledge
7. **LLM Judge** — auto-resolves high-confidence conflicts (configurable threshold)
8. **Knowledge Index** — auto-rebuilt after every ingestion
9. **Data Contract Validation** — webhooks validated before processing

### Contradiction Detection + LLM Judge

```
Doc A: "Cloud budget is 420,000 EUR" (Jan 2026)
Doc B: "Cloud budget increased to 550,000 EUR" (March 2026)
  ↓
Conflict detected (90% confidence)
  ↓
LLM Judge evaluates:
  - B is newer (March > January)
  - B from "Board Decision" (higher authority)
  - Recommendation: keep_b (92% confidence)
  ↓
92% > 85% threshold → AUTO-RESOLVED
Doc A superseded, excluded from search.
```

Configurable: `LLM_JUDGE_AUTO_RESOLVE_THRESHOLD=0.85` (higher = more conservative, 1.01 = never auto-resolve)

### 3-Layer Retrieval

```
Query: "What's our BI tool?"
  │
  ├─ Layer 1: Knowledge Index  (< 5ms)  → "SAP Analytics Cloud"
  ├─ Layer 2: Structured SQL   (< 20ms) → For aggregation queries
  └─ Layer 3: Hybrid Search    (200ms+) → Dense + BM25 + RRF + Feedback Boost
```

Pre-filter by `source_type` or `doc_type` (GIN indexes reduce vector scans ~90%).

### Performance Optimizations

| Feature | Impact |
|---------|--------|
| **Embedding Cache** | 30-50% fewer API calls (LRU, SHA-256 keyed) |
| **GIN Pre-Filter Indexes** | ~90% fewer vector scans |
| **Multi-Pass Claims** | +15-25% more claims extracted (`CLAIM_EXTRACTION_PASSES=2`) |
| **Feedback Boost** | Search results improve from user ratings |
| **Delta-Sync** | Notion connector only re-syncs changed pages |

## Dashboard

| Page | What It Shows |
|------|---------------|
| **Overview** | Document count, avg quality, open conflicts, pending reviews |
| **Upload** | Drag & drop (PDF, DOCX, XLSX, PPTX, CSV, HTML, TXT) |
| **Search** | Live search with routing info, confidence, source provenance |
| **Sources** | Connect Notion (token + sync), SharePoint/Jira (webhook setup) |
| **Documents** | Quality scores, conflict status, claims per document |
| **Conflicts** | LLM Judge recommendations, auto-resolve button, threshold selector |
| **Reviews** | Approve/reject with HTMX inline actions |
| **Analytics** | Quality by source, contradiction hotspots, claim stability |
| **Account** | API key management (create/revoke), quotas, usage metrics |

## API Reference

### Core

```bash
POST /v1/ingest                              # File upload
POST /v1/webhooks/ingest                     # Webhook connector
POST /v1/retrieve                            # 3-layer search
POST /v1/retrieve/feedback                   # Rate results
GET  /v1/documents                           # Cursor-paginated
GET  /v1/documents/{id}                      # Detail + chunks
DELETE /v1/documents/{id}                    # Soft delete
```

### Quality & Governance

```bash
GET  /v1/documents/{id}/quality              # Quality report
GET  /v1/conflicts                           # Detected contradictions
POST /v1/conflicts/{id}/judge                # Ask LLM Judge
POST /v1/conflicts/auto-resolve              # Auto-resolve all (threshold)
POST /v1/conflicts/{id}/resolve              # Manual resolution
GET  /v1/reviews                             # Review tasks
GET  /v1/claim-clusters                      # Multi-doc disagreements
```

### Knowledge Compilation

```bash
POST /v1/synthesis/compile                   # LLM topic compilation
POST /v1/synthesis/build-index               # Rebuild knowledge index
POST /v1/synthesis/curate                    # Full curation pipeline
GET  /v1/synthesis/{topic}                   # Topic summary
```

### Analytics & Operations

```bash
GET  /v1/analytics/quality-by-source         # Quality per source
GET  /v1/analytics/contradiction-hotspots    # Unstable knowledge
GET  /v1/analytics/claim-stability           # Claim churn
GET  /v1/analytics/audit                     # Audit log
GET  /v1/analytics/usage                     # Usage metrics
GET  /v1/source-tree                         # Hierarchical source view
GET  /metrics                                # Prometheus metrics
```

## MCP Server (AI Agent Integration)

```json
{
  "mcpServers": {
    "raasoa": {
      "command": "uv",
      "args": ["run", "python", "-m", "raasoa.mcp"],
      "env": { "RAASOA_URL": "http://localhost:8000" }
    }
  }
}
```

**11 Tools:** `raasoa_search`, `raasoa_ingest`, `raasoa_list_documents`, `raasoa_get_document`, `raasoa_quality_report`, `raasoa_list_conflicts`, `raasoa_auto_resolve`, `raasoa_feedback`, `raasoa_get_synthesis`, `raasoa_compile`, `raasoa_curate`

## Source Connectors

| Source | Method | Auto-Sync |
|--------|--------|-----------|
| **Notion** | Native (Dashboard → token → sync) | Delta-sync, scheduled |
| **SharePoint** | Native (MS Graph API) | On demand |
| **Jira / Confluence** | Webhook | Push-based |
| **Custom** | `POST /v1/webhooks/ingest` | Push-based |

Rich metadata extraction (Notion): author, editor, status, tags, parent path, timestamps.

## Embedding Providers

| Provider | Latency | Config |
|----------|---------|--------|
| **Ollama** (default) | ~2s/batch | `EMBEDDING_PROVIDER=ollama` |
| **OpenAI** | ~10ms/batch | `EMBEDDING_PROVIDER=openai` + API key |
| **Azure OpenAI** | ~10ms/batch | `OPENAI_BASE_URL=https://xxx.openai.azure.com/` |
| **Cohere** | ~15ms/batch | `EMBEDDING_PROVIDER=cohere` + API key |
| **Custom** | varies | `OPENAI_BASE_URL=http://your-server/v1` |

Embedding Cache wraps all providers — identical texts never embedded twice.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `EMBEDDING_PROVIDER` | `ollama` | `ollama` / `openai` / `cohere` |
| `AUTH_ENABLED` | `true` | API key authentication |
| `LLM_JUDGE_ENABLED` | `true` | AI conflict resolution |
| `LLM_JUDGE_AUTO_RESOLVE_THRESHOLD` | `0.85` | Auto-resolve confidence (0-1) |
| `CLAIM_EXTRACTION_PASSES` | `1` | 2 = multi-pass (+15-25% claims) |
| `QUALITY_GATE_ENABLED` | `true` | Quality scoring |
| `CONFLICT_DETECTION_ENABLED` | `true` | Contradiction detection |
| `DASHBOARD_PASSWORD` | — | Dashboard login password |

Full list in `.env.example`.

## Architecture

- **Single database**: PostgreSQL + pgvector + tsvector + pg_trgm. No Redis.
- **Local-first**: Default runs entirely local (Ollama + PostgreSQL). Cloud optional.
- **Embedding Cache**: LRU cache saves 30-50% of API costs.
- **GIN indexes**: Pre-filter before vector scan reduces work ~90%.
- **Tenant isolation**: Every query scoped to authenticated tenant.
- **Audit logging**: Every mutation logged (who, what, when, from where).
- **Job queue**: PostgreSQL-based (no Celery/Redis needed).
- **Prometheus metrics**: 12 operational metrics at `/metrics`.

## Development

```bash
uv sync --extra dev --extra parsing
docker compose up -d postgres
uv run alembic upgrade head
uv run uvicorn raasoa.main:app --reload --port 8000
uv run pytest -v              # 167 tests
uv run ruff check src/        # 0 errors
uv run mypy src/raasoa --ignore-missing-imports  # 0 errors
```

## License

Apache 2.0
