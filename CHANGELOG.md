# Changelog

## v0.1.0 — Knowledge Reliability Layer (2026-04-08)

First public release of RAASOA — the Knowledge Reliability Layer for enterprise AI agents.

### Core Features

- **3-Layer Retrieval**: Knowledge Index (<5ms) → Structured SQL (<20ms) → Hybrid Search (Dense + BM25 + RRF)
- **Quality Gates**: 7 automated checks per document, quality score 0-1, auto-quarantine
- **Claim Extraction**: LLM extracts Subject→Predicate→Value triples with temporal validity (valid_from/valid_until)
- **Contradiction Detection**: Claim-vs-claim + embedding similarity, confidence scoring
- **Human-in-the-Loop**: Review tasks, conflict resolution (keep_a/keep_b/both/dismiss), audit trail
- **Knowledge Compilation**: LLM-synthesized topic summaries, predicate normalization, knowledge index curator
- **Retrieval Feedback Loop**: Search result ratings improve future rankings
- **Source Pre-Filtering**: Filter by source_type or doc_type before vector search

### Document Formats

PDF, DOCX, XLSX, PPTX, CSV/TSV, HTML, TXT, Markdown — with table extraction

### Source Connectors

- **Notion**: Native connector — enter token in dashboard, click Sync
- **SharePoint**: Native connector via Microsoft Graph API
- **Jira / Confluence**: Webhook-based with setup guides
- **Custom**: Any system via POST /v1/webhooks/ingest
- **Data Contract Validation**: Content length, required fields, blocklist patterns

### Embedding Providers

Ollama (local, default), OpenAI, Azure OpenAI, Cohere, any OpenAI-compatible endpoint

### MCP Server (AI Agent Integration)

10 tools: search, ingest, list_documents, get_document, quality_report, list_conflicts, feedback, get_synthesis, compile, curate

### API

25+ REST endpoints covering ingestion, retrieval, documents, quality, conflicts, reviews, analytics, synthesis, ACL, sources, health, metrics

### Security & Compliance

- API key → tenant resolution (no client-settable tenant)
- Webhook secret verification (shared secret + HMAC support)
- Dashboard password protection
- Audit logging (who/what/when/where)
- GDPR hard-delete with per-tenant retention policies
- CORS middleware

### Operations

- Prometheus-compatible /metrics endpoint (12 metrics)
- PostgreSQL job queue (no Redis needed)
- Configurable DB pool size
- Health + readiness probes
- Docker Compose with API, PostgreSQL, Ollama

### Quality

- 155 tests passing
- ruff: 0 errors
- mypy strict: 0 errors
