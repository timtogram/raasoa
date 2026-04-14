# Changelog

## v0.2.0 — LLM Judge + SaaS Foundations (2026-04-14)

### LLM Judge for Conflict Resolution
- AI evaluates conflicting claims and recommends resolution (keep_a/keep_b/keep_both)
- Configurable auto-resolve threshold (default 0.85) — high-confidence conflicts resolved without human intervention
- Dashboard integration: Judge recommendations shown per conflict, "Auto-Resolve All" button with threshold selector
- Verdict stored in audit trail with reasoning
- MCP tool: `raasoa_auto_resolve`

### Performance Optimizations
- **Embedding Cache**: LRU cache saves 30-50% of embedding API calls (SHA-256 keyed dedup)
- **GIN Pre-Filter Indexes**: pg_trgm indexes reduce vector scans by ~90%
- **Multi-Pass Claim Extraction**: Optional second pass finds +15-25% more claims (`CLAIM_EXTRACTION_PASSES=2`)
- **Feedback Boost**: Retrieval feedback now actually applied to RRF scores (was stored but unused)

### SaaS Foundations
- **Tenant Signup**: `POST /v1/tenants` — public signup with free tier, returns API key
- **API Key Self-Service**: Create/list/revoke keys from dashboard or API
- **Usage Metering**: Track documents, queries, embedding calls, LLM calls per tenant
- **Quota Enforcement**: 429 when document/query/source limits exceeded
- **Plan Tiers**: free / starter / pro / enterprise with configurable limits
- **GDPR**: Data export (`POST /v1/tenants/me/export`) and right-to-erasure (`DELETE /v1/tenants/me`)
- **Account Dashboard**: Quotas with progress bars, usage table, key management UI

### Source Connectors
- **Notion**: Rich metadata extraction (author, editor, status, tags, parent path), delta-sync (only changed pages)
- **Scheduled Sync**: `sync_interval_minutes` per source, background worker polls automatically
- **Source Tree API**: Hierarchical view with quality overlay per folder/source

### Knowledge Governance
- **Claim Clusters**: Group ALL claims about the same topic across all documents (`GET /v1/claim-clusters`)
- **Context Annotations**: "Keep Both" resolution with context_a/context_b explaining WHY both are valid
- **Claim Deduplication**: Same fact from multiple chunks no longer creates duplicate claims
- **Page-Level Provenance**: Every search result includes page number, slide number, sheet name

### Enterprise
- **Audit Logging**: Every mutation logged (actor, action, resource, IP, timestamp)
- **Prometheus Metrics**: 12 operational metrics at `/metrics`
- **PostgreSQL Job Queue**: Async processing, no Celery/Redis needed
- **GDPR Retention**: Hard-delete after configurable retention period
- **DB Pool Configurable**: `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` env vars

### Quality
- 167 tests, ruff 0, mypy 0

---

## v0.1.0 — Knowledge Reliability Layer (2026-04-08)

First public release. Core features: 3-layer retrieval, quality gates, claim extraction,
contradiction detection, human-in-the-loop governance, knowledge compilation.
See [v0.1.0 release](https://github.com/timtogram/raasoa/releases/tag/v0.1.0).
