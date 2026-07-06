# AUDIT_AND_FIX_PLAN.md

> Production-readiness audit of RAASOA. Analysis-only; no code was changed to produce this.
> Findings verified against a live stack (Docker Postgres + pgvector, Ollama), the full
> test suite (256 passed / 4 skipped), and `ruff`/`mypy` (both clean). Every finding cites
> `file:line`. Fix tasks are scoped for independent execution by a fresh session.

## 1. Executive Summary

RAASOA is a "Knowledge Reliability Layer" — a FastAPI/Postgres+pgvector service that ingests
documents from connectors (Notion, SharePoint, Jira, HubSpot, uploads), scores their quality,
extracts claims, detects/resolves contradictions, and serves trusted retrieval to AI agents over
a REST API and an MCP server, with a Jinja dashboard on top. The **core happy path genuinely
works**: loading demo data, then `/v1/retrieve`, `/v1/answer` (correct cited German answer), and
`/v1/conflicts` all succeeded against a live stack, and the test suite passes against a real
Postgres. The recently-committed structured CRM query path (commit `bd83a9a`, which landed during
this audit) is wired, migrated, and tested — **not** the half-finished feature the stale git
snapshot suggested.

**Verdict: functionally close, security-and-deploy far.** The product demos well but is **not
shippable** as-is: the default deployment runs completely unauthenticated with wildcard CORS, the
dashboard mints real API keys to anonymous visitors, the flagship "per-agent clearance" MCP policy
gate is trivially bypassable and covers only 2 of 17 tools, and the README's headline 60-second
demo is broken in the documented Docker image. There is also a genuine privilege-escalation hole in
the ACL API. None of these are deep rewrites — they are gating, config, and error-handling fixes —
but until they land, deploying this exposes customer data.

**Top 5 blockers:**
1. **F-001** Dashboard is unauthenticated by default and mints tenant API keys to anonymous callers (verified live: minted a real `sk-` key with no auth).
2. **F-002** `POST/GET/DELETE /v1/acl` have no admin/principal check — any tenant key grants itself `admin` on any document (verified in code).
3. **F-004** `docker-compose.yml` ships `AUTH_ENABLED=false` **and** wildcard CORS with `allow_credentials=true` (verified live: `Origin: https://evil.test` reflected with credentials).
4. **F-003** "Load Demo Data" 404s in the documented Docker quickstart — `examples/` is not copied into the image (verified in Dockerfile).
5. **F-008** Any Ollama connection blip 500s `/v1/retrieve` and `/v1/answer` instead of degrading — the "honest refusal" promise doesn't cover the embedding call (verified in code).

---

## 2. Findings

### Severity table

| ID | Sev | Category | Location |
|----|-----|----------|----------|
| F-001 | Blocker | Auth bypass | dashboard/routes.py:33-40, 638-684; config.py:79; docker-compose.yml:95 |
| F-002 | Blocker | Privilege escalation | api/acl.py:32-76, 88, 122-148 |
| F-003 | Blocker | Broken headline feature | Dockerfile:14-23; dashboard/routes.py:844-851; README.md:60-65 |
| F-004 | Blocker | Insecure defaults | docker-compose.yml:91; main.py:41-54 |
| F-005 | Critical | MCP policy bypass | mcp/server.py:566-571, 957-960; mcp/policy.py:132-136 |
| F-006 | Critical | MCP policy bypass | mcp/server.py:517-545, 572, 601-603, 661, 685 |
| F-007 | Critical | Audit-trail fiction | mcp/policy.py:113; mcp/server.py:1086 vs api/analytics.py:17,145 |
| F-008 | Critical | Error handling / availability | providers/ollama.py:44,68; api/retrieval.py:65,284 |
| F-009 | Critical | ACL data leak | retrieval/knowledge_index.py:100-106; api/retrieval.py:112 |
| F-010 | Critical | ACL revocation leak | api/sources.py:1263-1284 |
| F-011 | Critical | SSRF | api/sources.py:918,975-989; create_source not admin-gated (208) |
| F-012 | Critical | Ranking correctness | retrieval/hybrid_search.py:178-204; confidence.py:30 |
| F-013 | Critical | Unsafe auto-resolution | quality/judge.py:294-308; ingestion/pipeline.py:343-360; quality/claim_conflicts.py:88-98 |
| F-014 | Critical | Config not wired | docker-compose.yml env whitelist (~80-100); config.py:5 |
| F-015 | Major | ACL bypass (read) | api/claim_clusters.py:61,129; versioning.py:34-85; source_tree.py:64; quality.py:35-152 |
| F-016 | Major | Authz gap (destructive) | api/documents.py:271-310 |
| F-017 | Major | Fail-open auth | middleware/auth.py:95-128; security/principal.py:83-104 |
| F-018 | Major | Confidence gate inert | retrieval/reranker.py:117-124; confidence.py:30 |
| F-019 | Major | Broken citations | retrieval/reranker.py:54-68, 146-158 |
| F-020 | Major | Quota bypass | api/retrieval.py:76-79 vs 284-338 |
| F-021 | Major | Provider broken | providers/cohere.py:37-55 |
| F-022 | Major | Connector data loss | api/sources.py:602-627, 719-741, 643-649 (Notion) |
| F-023 | Major | Broken maintenance job | ingestion/tiering.py:94-96 |
| F-024 | Major | Worker retry unused | worker/queue.py:84-111 |
| F-025 | Major | Deletion not propagated | api/webhooks.py:122-142; sources.py:1547-1570; retention.py:56-62 |
| F-026 | Major | Memory DoS | api/ingestion.py:86-98 |
| F-027 | Major | Stored XSS | templates/search.html:222-225, upload.html:127-137, sources.html |
| F-028 | Major | Dead UI buttons | templates/account.html:173; sources.html:283,309 |
| F-029 | Major | Stub tool | mcp/server.py:766-790 |
| F-030 | Major | False-positive conflicts | quality/conflicts.py:220-272 (identical text → conf 1.0) |
| F-031 | Major | Conflicts vs deleted docs | quality/conflicts.py:86-137; duplicate.py:34-45 |
| F-032 | Major | Index rebuild race | ingestion/pipeline.py:324-326; knowledge_index.py:124-166 |
| F-033 | Major | Schema drift (metadata) | models/__init__.py vs 11 migration-only tables; alembic/env.py:17 |
| F-034 | Major | Schema drift (dims) | models/chunk.py:37 vs migration 3a8758ffa2b0:179; .env.example:39 |
| F-035 | Major | Broken deploy docs | DEPLOYMENT.md:163-164 (bootstrap SQL); :223-238 ($@ discarded); :147-149 (signup path) |
| F-036 | Major | HubSpot ACL unusable | api/sources.py:1281 — no principal→`hubspot:owner:<id>` mapping |
| F-037 | Minor | Config default corrupt | migrations g7b8:57, j0c1:51 — `plan` stored as `'free'` w/ quotes |
| F-038 | Minor | Internal error leak | api/sources.py:552 |
| F-039 | Minor | Unbounded pagination | api/quality.py:81-83,127-128; analytics.py:104; dependencies.py:156; claim_clusters.py:34-84 |
| F-040 | Minor | Non-constant-time compare | middleware/auth.py:220 (webhook secret) |
| F-041 | Minor | Secrets in logs | docker-entrypoint.sh:6 (full DSN incl. password) |
| F-042 | Minor | Dead dependency | pyproject.toml:18 boto3 — zero imports; whole S3/MinIO stack unwired |
| F-043 | Minor | Missing auth header | mcp/server.py:719 (`raasoa_quality_report`) |
| F-044 | Minor | Stale published wheel | client/dist/*.whl differs from client/src at same version 0.1.0 |
| F-045 | Minor | CI not enforcing | ci.yml:58,62 (mypy + format `continue-on-error`) |
| F-046 | Minor | Doc / polish drift | tool count "16"/"15"/actual 17; SharePoint per-drive limit starvation; idempotency_key unused; frontmatter list values dropped; container runs as root |

### Detail on load-bearing findings

**F-001 — Dashboard unauthenticated by default; anonymous API-key minting.** `_check_auth`
(dashboard/routes.py:33-40) returns "OK" whenever `settings.dashboard_password` is empty, which is
the shipped default (config.py:79; compose passes `DASHBOARD_PASSWORD: ${DASHBOARD_PASSWORD:-}`).
Every `/dashboard/api/*` route is then open, bound to a hard-coded `DEFAULT_TENANT`. **Verified
live** with `AUTH_ENABLED=true`: `POST /dashboard/api/keys` returned a real key
`{"key":"sk-DvPbbdaZa..."}` with no credentials. The same surface exposes source creation with
plaintext secrets, ingest, and search. `_valid_sessions` is also an in-process `set`
(routes.py:30) — dashboard login breaks under the compose default `UVICORN_WORKERS: 2`.

**F-002 — `/v1/acl` privilege escalation.** `create_acl_entry` (api/acl.py:32-68) authenticates
with only `resolve_tenant_async`, checks the target document belongs to the tenant, then inserts a
**caller-controlled** `principal_id` and `permission`. **Verified by direct read.** Any tenant key —
including a scoped, non-admin personal key — can `POST {document_id, principal_id:"self",
permission:"admin"}` on any restricted document (visible or not) and self-grant access, defeating
the entire ACL model. `DELETE /v1/acl/{entry_id}` (122-148) lets the same caller strip others'
grants. Contrast the correctly-gated `update_source_visibility` (sources.py:384 uses `require_admin`).

**F-003 — Demo data missing from Docker image.** `dashboard/routes.py:844-851` reads samples from
`<repo>/examples/samples`, 404-ing "No sample documents bundled" when absent. `Dockerfile:14-23`
copies `src/`, `alembic/`, templates, entrypoint — **not `examples/`** — and compose has no bind
mount. **Verified in Dockerfile.** README.md:60-65 sells this as the first-run experience, so every
Docker quickstart user hits a dead button.

**F-004 — Insecure default deployment.** compose:91 sets `AUTH_ENABLED: ${AUTH_ENABLED:-false}`
(overriding the safe `config.py:78` default) and `.env.example:92` also ships `false`.
`_cors_origins()` (main.py:41-45) returns `["*"]` when unset, and the middleware sets
`allow_credentials=True, allow_methods=["*"]`. **Verified live**: `curl -H "Origin:
https://evil.test" /health` returned `access-control-allow-origin: https://evil.test` +
`access-control-allow-credentials: true`.

**F-005 / F-006 / F-007 — MCP policy gate.** (a) `requested = arguments.get("agent_clearance") or
env_default_clearance()` (server.py:566-571) — the "hard ceiling" comment is false; a caller passing
`agent_clearance:"secret"` sees everything. **Verified in code.** (b) The gate is applied only in
`raasoa_search` and `raasoa_get_skill`; `raasoa_answer`, `raasoa_get_document`,
`raasoa_list_documents`, `raasoa_doc_dependencies/diff` return content ungated — 2 of 17 tools.
(c) Policy denials/skill invocations POST to `{BASE_URL}/v1/audit` (policy.py:113, server.py:1086),
but the real route is `POST /v1/analytics/audit` (analytics.py:17,145) — **verified**: no `/v1/audit`
exists, failures are suppressed, so `/dashboard/audit` will never show an MCP event despite the docs.

**F-008 — Ollama outage 500s retrieve/answer.** `_embed_batch`/`_embed_one_by_one`
(ollama.py:44,68) catch only `httpx.HTTPStatusError`; a `ConnectError`/`ReadTimeout` (container
down) bypasses retries and the zero-vector fallback and propagates into both endpoints, which have
no try/except. **Verified in code.** The honest-refusal branches only wrap the *synthesis* call,
not the query-embedding call that precedes it.

**F-009 — Knowledge index leaks ACL-protected claims.** `build_index` (knowledge_index.py:100-106)
excludes only `source.default_visibility='restricted'`; documents protected by `acl_entries` rows on
a non-restricted source still enter the index, and `index_lookup` in `/v1/retrieve`
(retrieval.py:112) runs with **no principal filter**. Needs one E2E confirmation (see §3) but the
code path is clear.

**F-010 — HubSpot owner ACL not revoked on reassignment.** sources.py:1263-1284 deletes only the
grant for the *new* owner id before insert; when a record moves owners, the old
`hubspot:owner:<id>` `acl_entries` row survives, so the former owner keeps read access indefinitely.
`crm_objects.owner_principal_id` is correctly overwritten, so the two paths diverge.

**F-011 — SSRF via Jira `base_url`.** sources.py:918,975-989 POSTs to
`{base_url}/rest/api/3/search/jql` from tenant-supplied config with no scheme/host validation and
echoes `resp.text[:200]` back to the caller. `create_source` uses `resolve_tenant_async`, **not**
`require_admin` (confirmed), so any tenant key can point it at `http://169.254.169.254/…`.

**F-012 — Feedback boost defeats ranking and the confidence gate.** hybrid_search.py:178-204 adds
`AVG(rating)*0.1` (±0.1) to RRF scores whose max is ~0.0328, with **no query-similarity filter**
(contradicting feedback.py's own "for similar queries" doc; `get_feedback_boost` is dead code). One
thumbs-up pins a chunk to rank 1 for *every* query tenant-wide and saturates `retrieval_confidence`
(confidence.py:30 divides by 0.033), silently disabling the `/v1/answer` refusal gate.

**F-013 — Auto-resolve nukes whole documents unattended.** judge.py:294-308 sets an entire losing
document to `superseded` (removing it from search) from a single claim-pair verdict at ≥0.85; runs
on every ingest (pipeline.py:343-360). Worse, claim conflicts match on **predicate only, never
subject** (claim_conflicts.py:88-98), so unrelated facts ("IT response = 4h" vs "HR response = 24h")
can trigger it.

**F-014 — compose doesn't forward most settings.** The `api.environment` whitelist omits
`CORS_ORIGINS`, `SIGNUP_ENABLED`, `API_KEYS`, `RERANKER`, all `QUALITY_*`/`CONFLICT_*`/`LLM_JUDGE_*`,
rate limits, etc., and `.env` isn't copied into the image (config.py:5 `env_file=".env"` finds
nothing). So DEPLOYMENT.md's "set CORS_ORIGINS / disable signup" instructions are **unsatisfiable**
with the shipped compose file.

Remaining findings (F-015…F-046) are precisely located in the table above and preserved from the
sub-audits; the highest-value among them: ACL bypass on sibling read endpoints and unguarded
`delete_document` (F-015/F-016), fail-open identity resolution (F-017), reranker breaking confidence
and citations (F-018/F-019), Cohere provider dimension/input-type bug (F-021), Notion pagination +
delta-cursor data loss (F-022), broken tiering SQL (F-023), worker retry budget never used (F-024),
deletion not propagated to chunks/claims/ACL (F-025), unbounded upload read (F-026), stored XSS and
dead JS buttons in the dashboard (F-027/F-028), `find_by_metadata` stub (F-029), identical-text
false-positive conflicts (F-030), conflict passes comparing against deleted docs (F-031),
per-ingest full-index-rebuild race (F-032), and schema drift that would make
`alembic --autogenerate` emit destructive drops (F-033/F-034).

---

## 3. Needs Verification

- **F-009 doc-level ACL leak** — add a test: ingest a doc from a non-restricted source, add an
  `acl_entries` row scoping it to `user:alice`, build the index, then call `/v1/retrieve` as
  `user:bob` and assert the claim is absent from `index_hits`.
- **retrieval_logs.chunks_returned column type** (retrieval.py:225-249 binds a Python list) — check
  the creating migration; if `jsonb`, asyncpg rejects the list and every retrieval-log write silently
  rolls back. Confirm with one live request + `SELECT count(*) FROM retrieval_logs`.
- **Zero-vector partial-embed publish** — confirm whether any provider returns zero-vectors instead
  of raising; if all raise, the `embedded_count` guard (pipeline.py:260) is dead defensive code.
- **`ghcr.io/timtogram/raasoa:latest`** (DEPLOYMENT.md:96) — CI never pushes (ci.yml:88 `push:false`)
  and there's no release workflow; confirm with `docker manifest inspect`.
- **`uv run` at container start with no network** — `docker run --network none raasoa:latest`; if it
  fails at `uv run alembic`, add `--frozen --no-sync`.
- **HubSpot owner identity mapping (F-036)** — `grep -rn "hubspot:owner" src/`: confirm nothing maps
  a real principal to the synthetic owner id (making the feature "complete but manual", or dead for
  end users).

**Resolved during audit** (originally suspected issues, checked and cleared): migration graph has
exactly one head `n4a5b6c7d8e9`, no fork problem (the g7a8/g7b8 fork is correctly merged by
`19dc365e7974`); `pg_trgm` extension **is** created (migrations g7a8/l2e3) and installed, so the
Graph page does not 500; the CRM path is committed and functional (not a blocker); `boto3` confirmed
unused (F-042); `tenants.plan` default confirmed corrupt (F-037); `api_keys.id`/`key_prefix` confirmed
have no defaults (root of the broken bootstrap SQL, F-035) — **since fixed**: `key_prefix` genuinely
has no default and must be supplied by callers (see the corrected `DEPLOYMENT.md` bootstrap SQL under
T-26); `id` now has `server_default=gen_random_uuid()` as of migration `q7d8e9f0a1b2` (T-25), matching
the other 10 new tables and the `UUIDMixin` base class it was already silently assuming.

---

## 4. Fix Plan

Ordered blockers-first, then dependency order. Each task is scoped to ~1–2 files for a fresh session.
"Verify" assumes `docker compose up -d postgres` + `uv run alembic upgrade head`.

### Phase A — Security blockers (do first; independent) — ✅ DONE (2026-07-05)

- **T-01 (F-002)** — ✅ `api/acl.py` gated behind `require_admin` (admin-capable caller + tenant
  `admin_api_enabled` opt-in, matching `update_source_visibility`'s existing pattern). Regression
  tests: `tests/test_api/test_acl_admin_gate.py`.
- **T-02 (F-001)** — ✅ `dashboard/routes.py`'s `_check_auth` now fails closed: when
  `AUTH_ENABLED=true` and `DASHBOARD_PASSWORD` is unset, every dashboard route (including
  `/api/keys`) returns 401/503 instead of silently allowing access; `/login` no longer redirect-loops
  in that state; an empty submitted password can no longer match an empty configured password.
  `AUTH_ENABLED=false` (the documented local/self-hosted demo posture) is unchanged — dashboard stays
  open, as the README's "Load Demo Data" quickstart requires. Regression tests:
  `tests/test_api/test_dashboard_auth_gate.py`.
- **T-03 (F-004, F-014)** — ✅ `docker-compose.yml` now forwards the ~25 previously-dropped settings
  (`CORS_ORIGINS`, `SIGNUP_ENABLED`, `API_KEYS`, all `QUALITY_*`/`CONFLICT_*`/`CLAIM_EXTRACTION_*`/
  `LLM_JUDGE_*`, `RERANKER`, rate limits, `MCP_HTTP_ENABLED`/`MCP_INTERNAL_URL`, `DB_POOL_SIZE`/
  `DB_MAX_OVERFLOW`) to `api` and `scheduler`, verified via `docker compose config`. Did **not** flip
  the `AUTH_ENABLED` default to `true` — that would break the documented zero-login demo flow and is
  the product-scope call flagged in Open Question 1; instead both `docker-compose.yml` and
  `.env.example` now carry an explicit comment telling operators what to set
  (`AUTH_ENABLED`/`DASHBOARD_PASSWORD`/`CORS_ORIGINS`/`SIGNUP_ENABLED`) before exposing this on the
  public internet.
- **T-04 (F-004 code)** — ✅ `main.py` no longer combines `allow_origins=["*"]` with
  `allow_credentials=True` — credentials are only sent when `CORS_ORIGINS` is explicitly configured.
  Verified live: default config now returns `access-control-allow-origin: *` with no
  `access-control-allow-credentials` header. Regression tests: `tests/test_api/test_cors_credentials.py`.
- **T-05 (F-011)** — ✅ New `connectors/net.py` (`validate_outbound_url`) blocks non-https schemes and
  private/loopback/link-local/reserved/multicast resolved addresses; wired into both `create_source`
  (immediate feedback) and `_sync_jira` (defense-in-depth against configs created before this fix or
  DNS changes). `create_source` now requires an admin-capable caller — but deliberately *without* the
  extra `admin_api_enabled` tenant opt-in `require_admin` normally checks, since two existing tests
  (`test_hubspot_source_defaults_to_restricted`, `test_notion_source_defaults_to_inherit`) establish
  that a tenant's own master key must be able to create sources without first opting into the
  delegated Admin API — that flag governs personal-key delegation, not basic tenant operations.
  Regression tests: `tests/test_connectors/test_net.py`, `tests/test_api/test_create_source_admin_gate.py`.

**Verification:** all fixes confirmed both by new automated tests and by re-running the exact live
`curl` reproductions from the findings above — each now returns 401/400 instead of succeeding. Full
suite: 305 passed, 4 skipped (up from 256 at audit time), `ruff check` and `mypy --strict` clean. The
documented demo flow (`docker compose up -d` → dashboard → Load Demo Data → `/v1/retrieve` →
`/v1/answer`) was re-verified end-to-end after all five fixes and still works.

**Not yet done (at the time Phase A completed):** Phases B–E (T-06 through T-28), as does the
AUTH_ENABLED-by-default product decision in Open Question 1. *(Update: Phases B and C are now also
done — see below.)*

### Phase B — MCP governance — ✅ DONE (2026-07-05)

- **T-06 (F-005)** — ✅ New `policy.py::effective_clearance()` clamps every `agent_clearance` request
  to `min(requested, env_default_clearance())` — a request for a higher clearance than the
  server-side ceiling is silently clamped down, never honored. All four call sites in `server.py`
  (`raasoa_search`, `raasoa_list_documents`, `raasoa_get_document`, `raasoa_get_skill`) now go
  through this helper instead of the old `arguments.get(...) or env_default_clearance()` pattern that
  let any caller-supplied value override the ceiling outright. Verified live: with the ceiling at its
  default (`public`), requesting `agent_clearance: "secret"` still only returns public documents;
  raising `RAASOA_MCP_DEFAULT_CLEARANCE=internal` then correctly unlocks `internal`-classified content.
- **T-07 (F-006)** — ✅ Policy filtering extended to `raasoa_list_documents`, `raasoa_get_document`,
  `raasoa_answer` (a denied citation refuses the *whole* answer, since the synthesized prose was
  generated from that source and can't be safely redacted after the fact), and `raasoa_doc_dependencies`
  / `raasoa_doc_diff` (via a new `_clearance_denial_for_document()` pre-check against the root
  document, since those two endpoints' REST responses don't themselves carry classification metadata).
  This required a small, additive schema change: `doc_metadata` was added to `DocumentSummary` /
  `DocumentWithChunks` (`schemas/document.py`, `api/documents.py`) and to `AnswerCitation` /
  `SourceChunk` (`schemas/retrieval.py`, `retrieval/answer.py`, `api/retrieval.py`) — both endpoints
  already had the data (`documents.doc_metadata`, `SearchResult.doc_metadata`) but weren't returning
  it. **Deliberately left out of scope:** the `structured` answer block in `raasoa_search` — it's
  built from typed CRM/DB queries, not documents, and carries no classification concept in its
  current schema; gating it would require a separate, larger schema change to the structured-query
  path and is not done here.
- **T-08 (F-007)** — ✅ Both audit call sites (`policy.py::audit_denials`, and the skill-invocation
  audit in `server.py`) now POST to `/v1/analytics/audit` (the real endpoint) instead of the
  nonexistent `/v1/audit`; the skill-invocation call site also no longer swallows failures via a bare
  `contextlib.suppress(Exception)` — it now logs a warning on both HTTP-level and exception failures,
  matching `audit_denials`' existing pattern.
- **T-09 (F-029, F-043)** — ✅ `raasoa_find_by_metadata` now calls `POST /v1/find_by_metadata` (the
  real server-side filter, which already existed and was simply never called) instead of listing all
  documents and ignoring the `metadata` argument; its results are now also policy-gated.
  `raasoa_quality_report` now sends `headers=_headers()` like every other tool call — previously the
  only one that didn't, so it 401'd under `AUTH_ENABLED=true`.

**Verification:** 24 new tests — pure unit tests for `effective_clearance`/`hit_is_allowed`/
`apply_policy_gate` (`tests/test_mcp/test_policy.py`) and `httpx.MockTransport`-based wiring tests for
every changed tool (`tests/test_mcp/test_policy_gate_wiring.py`), plus live end-to-end verification
against a running instance with demo data (tagging a document `classification: secret` and confirming
`raasoa_get_document`/`raasoa_list_documents` correctly hide it, and that requesting a higher
clearance than the server ceiling has no effect). Full suite: 333 passed, `ruff` and `mypy --strict`
clean.

**Not yet done (at the time Phase B completed):** Phases C–E, the `structured`-block gating noted
above, and the Open Questions in §5. *(Update: Phase C is now also done — see below.)*

### Phase C — Retrieval / quality correctness — ✅ DONE (2026-07-05)

- **T-10 (F-008)** — ✅ `providers/ollama.py` now catches `httpx.TransportError` (connection-level
  failures) separately from `httpx.HTTPStatusError`, retries the same as before, and on exhaustion
  raises a new `EmbeddingProviderUnavailableError` (`providers/base.py`) instead of silently
  degrading to zero vectors for a total outage. `api/retrieval.py` catches it in both `retrieve()`
  (falls back to index/structured results if any, else 503) and `answer()` (honest refusal). Verified
  live: pointing `OLLAMA_BASE_URL` at an unreachable host now returns 503 from `/v1/retrieve` and a
  clean refusal from `/v1/answer`, not a 500.
- **T-11 (F-012)** — ✅ The feedback boost multiplier in `hybrid_search.py`'s CTE dropped from `0.1` to
  `0.003` (~9% of RRF's ~0.033 ceiling instead of ~3x it) — still a real nudge among near-ties, never
  enough to override actual relevance for an unrelated query. True query-similarity scoping (the
  aspirational design in `feedback.py`'s docstring) was not implemented — a much larger feature
  requiring embedding comparison against historical query text; capping was the scoped, low-risk fix
  the task explicitly allowed.
- **T-12 (F-018, F-019)** — ✅ `CrossEncoderReranker`/`OllamaReranker` now rebuild `SearchResult` via
  `dataclasses.replace()` instead of by hand, preserving all fields (title, url, doc_metadata, etc.)
  through reranking. Each reranker now declares `SCORE_SCALE` (0.033 for passthrough/RRF, 1.0 for the
  two LLM/cross-encoder rerankers); `compute_confidence()` takes an explicit `max_score` parameter and
  both call sites in `api/retrieval.py` pass `reranker.SCORE_SCALE`.
- **T-13 (F-013, F-030)** — ✅ Four coordinated changes: (1) `claim_conflicts.py` now requires
  subject match (not just predicate similarity) before flagging a contradiction — "IT dept response
  time" vs "HR dept response time" no longer false-positives; (2) it now stores each claim's `id` in
  the conflict's `details` JSONB; (3) `judge.py::auto_resolve_conflicts` reads those ids and
  supersedes only the one losing **claim**, never the whole document or its other claims — a conflict
  row from before this fix (no claim ids in `details`) is left for human review rather than falling
  back to the old whole-document behavior; (4) `conflicts.py`'s semantic-contradiction pass now skips
  chunks whose `content_hash` matches the current chunk's own hash (identical text is a duplicate,
  never a "contradiction"). Additionally, `settings.llm_judge_enabled` defaulted to `false`
  (`config.py`, `.env.example`, both `docker-compose.yml` service blocks) — unattended, permanent
  claim supersession required an explicit opt-in. **Reversed 2026-07-06 per product decision**: the
  owner chose automatic conflict resolution by default (open question #2 in §5), so
  `llm_judge_enabled` now defaults to `true` in all three places — every ingest with a
  `conflicts_detected` document runs `auto_resolve_conflicts` unattended, best-effort
  (`try/except: pass`), superseding claims above `LLM_JUDGE_AUTO_RESOLVE_THRESHOLD` (0.85 default) and
  leaving everything below it for human review as before. The mechanism itself (subject-match
  requirement, per-claim scoping, duplicate-hash exclusion) is unchanged — only the default toggle
  flipped. Verified this doesn't break the demo: `/v1/answer`
  still correctly cites the newer document via RAG ranking regardless of claim-supersession state
  (confirmed live before and after), and the demo's meal-allowance conflict now stays visible under
  `/v1/conflicts` for the user to review, matching the README's own description of the flow.
  **A serious latent bug surfaced immediately on flipping the default** — because
  `settings.llm_judge_enabled and settings.conflict_detection_enabled and doc.conflict_status ==
  "conflicts_detected"` short-circuits on the first condition, `doc.conflict_status` was NEVER
  evaluated while the setting defaulted to `False`, so this bug was completely dormant since T-13
  landed. `detect_claim_conflicts` sets `conflict_status` via a raw SQL `UPDATE` (bypassing the ORM
  identity map), and the several `session.commit()` calls earlier in `ingest_file` expire `doc`'s
  attributes by SQLAlchemy default — reading an expired attribute triggers an implicit lazy-reload,
  which isn't supported under `AsyncSession` and raises `sqlalchemy.exc.MissingGreenlet`. With the
  default flipped to `true`, this would have 500'd every single ingest that reached this line — i.e.
  most real ingests with `conflict_detection_enabled` on. Caught immediately because the full test
  suite was re-run after the flip (`tests/test_api/test_ingestion.py` and
  `tests/test_api/test_webhook_idempotency.py` both failed with this exact error) rather than trusting
  the config change in isolation. Fixed in `pipeline.py` by adding an explicit `await
  session.refresh(doc)` before reading `conflict_status`, inside the same best-effort `try/except`.
  New regression test `tests/test_ingestion/test_llm_judge_pipeline_integration.py` reproduces the
  exact scenario (mocking claim extraction/conflict-detection/judge to avoid needing a real LLM call)
  and, via `git stash`, was confirmed to fail with the identical `MissingGreenlet` traceback against
  the pre-fix code.
- **T-14 (F-009)** — ✅ `knowledge_index.py::build_index` now also excludes any document with its own
  `acl_entries` row (regardless of source `default_visibility`) — previously only restricted-*source*
  claims were excluded, so a document individually ACL-protected on an otherwise-open source still
  leaked its claims through the index's principal-agnostic fast path.
- **T-15 (F-020)** — ✅ `/v1/answer` now calls `check_quota(..., "queries")` before running, same as
  `/v1/retrieve` — previously it only tracked usage after the fact, so an over-quota tenant could
  switch endpoints to keep querying.
- **T-16 (F-021)** — ✅ `CohereEmbeddingProvider.embed()` now sends `output_dimension` (fixing the
  768-vs-1536 dimension mismatch that broke every ingest under `EMBEDDING_PROVIDER=cohere`) and an
  `input_type` parameter threaded through the whole chain (`EmbeddingProvider` protocol → `Ollama`/
  `OpenAI` providers accept-and-ignore it → `EmbeddingCache` includes it in the cache key → the one
  query-embedding call site in `hybrid_search.py` passes `"search_query"` instead of the
  always-`"search_document"` default).
- **T-17 (F-015, F-016)** — ✅ Applied `acl_predicate_sql`/`resolve_principal_ids` to
  `claim_clusters.py` (both endpoints), `versioning.py` (both endpoints), `source_tree.py` (both the
  source-level aggregation and per-source document listing), and `quality.py`
  (`get_document_quality`, `list_quality_findings`, and `list_conflicts` — the last using the same
  "visible on at least one side" policy already established and tested in `structured.py`'s conflict
  summary, for consistency). `documents.py::delete_document` now requires the caller to be able to
  *see* the document (same 404-not-403 semantics as `get_document`) before deleting it.
- **T-18 (F-017)** — ✅ `_resolve_key_row_from_db` (`middleware/auth.py`) no longer swallows
  exceptions — a DB error during identity lookup now propagates. `resolve_principal_async`
  (`security/principal.py`) catches it explicitly and raises `HTTPException(503)` instead of falling
  through to the legacy-tenant-wide-admin branch, which previously silently upgraded a scoped
  personal key to unfiltered admin access on a transient infrastructure failure.

**Verification:** 34 new tests across 9 new test files (T-10 through T-18 combined), each confirmed to
fail against the pre-fix code via `git stash` and pass after restoring the fix. Full suite: 366
passed (up from 256 at audit start), `ruff` and `mypy --strict` clean (mypy scoped to `src/`, matching
this project's own CI). Live end-to-end re-verification: Ollama-outage degradation, ACL-scoped read
endpoints, and the full demo flow (load demo data → retrieve → answer, with the conflict now staying
open for review instead of auto-resolving).

### Phase D — Connectors & worker — ✅ DONE (2026-07-05)

- **T-19 (F-022)** — `api/sources.py`. Paginate Notion search (`next_cursor`/`has_more`); set the
  delta cursor from the max `last_edited_time` seen, not wall clock; normalize timestamp formats.
  **Accept:** a >100-page workspace ingests all pages across runs. **M / medium.** ✅ Implemented:
  `while True` loop follows `next_cursor`/`has_more`; a new `_normalize_timestamp()` helper
  canonicalizes Notion's `...Z` vs. the codebase's `+00:00` offset form before comparing/max()-ing,
  and the cursor write uses the max `last_edited_time` seen across processed pages (never wall
  clock), with a guard against the cursor moving backward. **Verified:** full diff read; confirmed
  via direct code inspection that `_normalize_timestamp` delegates to the existing `_parse_datetime`
  helper and re-renders via `.isoformat()`.
- **T-20 (F-010, F-025)** — `api/sources.py`, `api/webhooks.py`, `worker/retention.py`. On owner
  reassignment delete the *old* owner grant; on delete cascade to (or filter) chunks/claims/acl_entries/crm_objects.
  **Accept:** reassigned HubSpot record drops the old owner's access; deleted doc's ACL rows are gone.
  **M / medium.** ✅ Implemented: HubSpot owner reassignment now revokes *any* prior
  `hubspot_owner:%` grant, not just today's specific owner_id; a new shared
  `_cascade_delete_document_data()` helper (used by both the SharePoint webhook delete path and
  `api/webhooks.py`'s `document.deleted` handler) deletes `acl_entries`/`crm_objects`/`chunks`/`claims`
  rows for the affected document IDs; `worker/retention.py`'s purge loop gained
  `acl_entries_purged`/`crm_objects_purged` stats and a latent substring-match bug (`"chunks" in
  table` matching unintended tables) was fixed to an exact `table == "chunks"` check. **Verified:**
  full diff read for all three files; ran the relevant test suites; confirmed via `git stash` that
  `tests/test_api/test_deletion_cascade.py` and the retention tests fail without the fix and pass
  with it restored.
- **T-21 (F-023, F-024)** — `ingestion/tiering.py`, `worker/queue.py`. Bind the interval param
  correctly (`now() - (:cold_days || ' days')::interval`); honor `max_attempts` before marking
  `failed`. **Accept:** `run_tiering_sweep` executes without interval syntax error; a failing job
  retries up to `max_attempts`. **S / low.** ✅ Implemented in Phase C's push (see above); re-confirmed
  present and covered by `tests/test_ingestion/test_tiering.py` and `tests/test_ingestion/test_worker_queue.py`,
  both green in the final full-suite run.
- **T-22 (F-031, F-032)** — `quality/conflicts.py`, `duplicate.py`, `ingestion/pipeline.py`. Filter
  `status != 'deleted'` in conflict/dup queries; make the index update incremental per-document (or
  lock) instead of a full tenant rebuild. **Accept:** re-uploading a deleted doc isn't flagged as its
  own duplicate; concurrent ingests don't corrupt the index. **M / medium.** ✅ Implemented: all four
  conflict-detection passes (`_detect_exact_duplicates`, `_detect_chunk_overlap`,
  `_detect_title_supersession`, `_detect_semantic_contradictions`) and both `duplicate.py` checks now
  exclude `status = 'deleted'` and quarantined/rejected/superseded review statuses. The knowledge
  index gained `update_index_for_document(session, tenant_id, document_id)` — an incremental,
  per-document update that recomputes only the affected `(subject, predicate)` keys (including keys
  that reference the document being retracted) instead of the previous full-tenant rebuild;
  `ingestion/pipeline.py` step 13 now calls this instead of `build_index()`, which is kept for the
  standalone worker job / manual admin use. **Verified:** full diff read for both quality files; ran
  19 tests across `test_deleted_doc_exclusion.py`, `test_knowledge_index_incremental.py`,
  `test_knowledge_index.py`, `test_knowledge_index_acl.py` — all green; confirmed via `git stash` that
  3/6 conflict tests fail without the status filter. Note: the incremental update narrows but doesn't
  fully eliminate a rare same-key concurrent-write race — judged an acceptable improvement matching
  the acceptance criterion's intent (no more full-tenant corruption) without adding advisory-locking
  complexity that wasn't asked for.

**Verification:** every T-19–T-22 file diff was read in full (not sampled); each fix's test file was
run individually and, via `git stash push -- <file>` / run test / `git stash pop`, confirmed to fail
against the pre-fix code and pass with the fix restored — this is the same rigor as Phase C, applied
independently after the fixing agents reported done, per the standing "trust but verify" approach for
this audit.

### Phase E — Data hygiene, deploy, docs — ✅ DONE (2026-07-05)

- **T-23 (F-026)** — `api/ingestion.py`. Enforce size limit via streaming / `Content-Length` before
  reading the whole body. **Accept:** oversized upload rejected without buffering it all. **S / low.**
  ✅ Implemented: reads in bounded 1MB chunks, checking cumulative size against `max_file_size_mb` on
  every iteration and raising 413 as soon as the threshold is crossed, instead of the previous
  `await file.read()` that buffered the entire body before any check ran. **Verified:** new
  `tests/test_api/test_ingestion.py` asserts the handler performs ≤3 chunk reads and never reads the
  full oversized body — confirmed passing with the fix and, via `git stash`, failing without it
  (`assert 6291456 < 6291456` — proving the old code read the entire 6MB payload before rejecting).
- **T-24 (F-027, F-028)** — `templates/search.html`, `upload.html`, `sources.html`, `account.html`.
  Escape user content in JS `innerHTML` sinks (use `textContent`/escape helper); fix the `\\'`
  escaping in revoke/sync onclick handlers. **Accept:** a doc titled `<img onerror>` doesn't execute;
  Revoke and post-connect Sync buttons work. **M / medium.** ✅ Implemented: an `escapeHtml()` helper
  in all four templates now wraps every interpolated value in `innerHTML` template strings;
  `search.html` gained a `safeHref()` that only allows `http:`/`https:` URLs (rejects
  `javascript:`/`data:`) for the "Open source" link, plus `encodeURIComponent` on document IDs used
  in path segments; the fragile `onclick="revokeKey(\\'...\\')"`/`onclick="syncSource(\\'...\\')"`
  string-concatenation patterns were replaced with `data-key-id`/`data-sync-source-id` attributes plus
  a delegated `document.addEventListener('click', ...)` handler. **Verified live in-browser**
  (`preview_start`/`preview_eval`/`preview_click`/`preview_screenshot` against the running dashboard,
  not just static review): injected `<img src=x onerror=...>`, `<script>`, `<svg onload=...>`,
  `javascript:` URLs, and attribute-breakout payloads (`id-"><script>...`) into mocked API responses
  for all four pages; confirmed `window.__xssFired` never fired, all payloads rendered as literal
  escaped text in the DOM (screenshot captured), the malicious `javascript:` source link was omitted
  entirely by `safeHref`, and the click-delegation handlers correctly extracted the raw (HTML-decoded)
  id from the `data-*` attribute without executing the embedded script.
- **T-25 (F-033, F-034, F-037)** — `models/*.py`. Add ORM models/columns for the 11 migration-only
  tables + `admin_api_enabled`/`default_visibility`; make `chunk.embedding` dimension consistent with
  config across model and migration; fix the `plan` server default (`'free'` → `free`). **Accept:**
  `alembic revision --autogenerate` produces an empty diff. **L / medium.** ✅ Implemented: 11 new ORM
  model classes across 5 new files (`crm.py`, `feedback.py`, `ops.py`, `principals.py`, `saas.py`);
  `default_visibility` (Source) and `admin_api_enabled` (Tenant) columns added; `chunk.py` gained a
  detailed comment documenting that `Vector(settings.embedding_dimensions)` reflects the model's
  expectation, not a dynamically-resizable column — the actual Postgres column is fixed at `dim=768`
  by migration `3a8758ffa2b0` and switching `EMBEDDING_PROVIDER` alone will not resize it (confirmed
  the migration's hardcoded width matches `config.py`'s default). The `tenants.plan` double-quoted
  `server_default` bug was fixed via new migration `o5b6c7d8e9f0` (data repair + `server_default =
  sa.text("'free'")`). **Verified, with a real gap found and closed during independent review:** ran
  `alembic revision --autogenerate` before and after — zero `drop_table`/`add_column`/`drop_column`
  entries (only pre-existing, out-of-scope index-declaration gaps on older tables, unrelated to this
  audit). While verifying, discovered the exact same double-quoting bug pattern
  (`server_default="'word'"`) affected three more columns the original fix didn't cover
  (`knowledge_index.status`, `knowledge_syntheses.status`, `job_queue.status`) — confirmed by
  compiling each column's DDL directly against current migration-file content rather than trusting
  this one long-lived dev database's current state (a migration file can be edited after a table was
  first created without the live table ever seeing the new DDL). Wrote a follow-up migration
  `p6c7d8e9f0a1` fixing all three, updated the corresponding model files
  (`feedback.py`/`ops.py`/`tenant.py`) to use `server_default=text(...)`, and corrected an
  over-cautious comment on `principal_groups.origin` that had incorrectly flagged it as having the
  same risk (proved via DDL compilation that its unquoted `server_default="manual"` does not
  reproduce the bug). Applied both migrations via `alembic upgrade head` and confirmed live via
  `psql` that all four columns now show correctly single-quoted defaults. **A second, separate gap**
  surfaced from the same verification pass: of the 11 new tables, only 4 (`crm_objects`,
  `principal_groups`, `principal_memberships`, `source_acl_grants`) had their migration set
  `id`'s `server_default=gen_random_uuid()` — the other 7 (`retrieval_feedback`,
  `knowledge_syntheses`, `knowledge_index`, `audit_events`, `job_queue`, `api_keys`, `usage_events`)
  didn't, even though every one of the 11 models inherits `UUIDMixin`, which declares that default
  unconditionally — a real model/migration mismatch autogenerate never flagged because
  `compare_server_default` isn't enabled in `alembic/env.py`. Checked every actual `INSERT INTO` call
  site for all 7 tables (11 call sites across `retrieval/feedback.py`, `quality/synthesis.py`,
  `retrieval/knowledge_index.py`, `middleware/audit.py`, `worker/queue.py`, 4 sites for `api_keys`
  across `api/keys.py`/`api/admin.py`/`api/tenants.py`/`dashboard/routes.py`,
  `middleware/metering.py`) — every one already supplies `id` explicitly via Python's `uuid.uuid4()`,
  so this was never a live bug, only latent drift. Added migration `q7d8e9f0a1b2` to bring all 7 in
  line with the other 4 and with the models; applied via `alembic upgrade head` and confirmed live
  via `psql` that all 7 now show `gen_random_uuid()` as their column default. Re-ran
  `alembic revision --autogenerate` once more afterward — still zero `add_column`/`drop_column`/
  `drop_table` entries.
- **T-26 (F-035, F-003)** — `Dockerfile`, `docker-entrypoint.sh`, `DEPLOYMENT.md`. Copy `examples/`
  into the image (fixes F-003); make the entrypoint pass `"$@"` so `docker compose run api alembic …`
  works; correct the bootstrap-key SQL and the signup path in docs. **Accept:** Load Demo Data works
  in a built image; documented ops commands run as written. **M / medium.** ✅ Implemented: Dockerfile
  now `COPY examples/ examples/`; `docker-entrypoint.sh` masks the DSN in startup logs (shows only
  `host:port/db`, never credentials) and execs any passed-through command (`if [ "$#" -gt 0 ]; then
  exec "$@"; fi`) after the DB-wait loop instead of always running the default migrate-then-serve
  flow; `DEPLOYMENT.md` corrected the signup endpoint (`/v1/tenants`, not `/v1/tenants/signup`), fixed
  the bootstrap `api_keys` INSERT to supply the required `key_prefix` column and use
  `encode(digest(...), 'hex')` matching `_hash_key`'s actual hex-string format (plus a
  `pgcrypto`-extension note), and corrected the retention-purge command to use `--entrypoint` since
  the `scheduler` service pins a hard `entrypoint:` in `docker-compose.yml` that a bare trailing
  command would be appended to, not replace. **Verified independently, not just trusting the prior
  claim:** ran a real `docker buildx build --platform linux/amd64 --load .`; confirmed `examples/`
  (including `samples`) present in the built image via `docker run --entrypoint ls`; ran the full
  entrypoint flow against the real Postgres container (`docker run --network raasoa_default ...`) and
  confirmed the masked log line, DB-wait, and command-passthrough all work as documented. Also fixed
  one further doc inaccuracy found during verification: the comment claiming `api_keys.id` "has no
  column default" was wrong (it has `server_default=gen_random_uuid()`, a Postgres 13+ builtin) —
  corrected to note `id` is optional while `key_prefix` genuinely has no default.
- **T-27 (F-041, F-042, F-045)** — `docker-entrypoint.sh`, `pyproject.toml`, `.github/workflows/ci.yml`.
  Stop echoing the DSN; remove `boto3`+S3 config (or wire artifact storage); make mypy/format gate CI
  now that both pass. **Accept:** startup logs contain no password; CI fails on a type error. **S / low.**
  ✅ Implemented: DSN masking (see T-26); `boto3` removed from `pyproject.toml`/`uv.lock`; CI's mypy
  step no longer has `continue-on-error: true`. **Verified:** grepped `src/`+`tests/` for any
  remaining `boto3`/`import boto` references (none); ran `uv run mypy src/raasoa` locally to confirm
  it's actually clean before trusting the hard CI gate (`Success: no issues found in 109 source
  files`) — hardening the gate without first confirming this would have been a real risk of breaking
  CI on the next push.
- **T-28 (F-038, F-039, F-040, F-044, F-046)** — Minor polish batch: generic 500 detail in
  sources.py:552; `Query(ge=…, le=…)` bounds on unbounded list params; `hmac.compare_digest` for the
  webhook secret; rebuild/version-bump the client wheel; reconcile tool-count docs; add a `USER`
  directive to the Dockerfile; register the `requires_ollama` pytest marker. **Accept:** ruff/mypy/tests
  green; params bounded. **S / low.** ✅ Implemented: `sources.py:552` now returns a generic message
  instead of leaking the internal exception string; `Query(ge=..., le=...)` bounds added across
  `quality.py`, `analytics.py`, `claim_clusters.py`, `dependencies.py` (new
  `tests/test_api/test_list_endpoint_bounds.py`, 8 tests, no live DB required since FastAPI validates
  bounds pre-handler); `middleware/auth.py`'s webhook-secret comparison now uses
  `hmac.compare_digest` (confirmed the empty-secret case is handled *before* reaching
  `compare_digest`, so there's no bypass risk from comparing against an empty string);
  `client/pyproject.toml` bumped 0.1.0 → 0.1.1 and the wheel/sdist rebuilt (gitignored, not
  committed); `mcp/http_transport.py`/`README.md`/`INTEGRATIONS.md` tool count corrected 15/16 → 17
  (confirmed by counting the actual `"name": "raasoa_*"` tool definitions in `mcp/server.py`); a
  non-root `USER raasoa` directive added to the Dockerfile (see verification below).
  **Investigated and found two items from T-28's own list needed independent completion:**
  the `requires_ollama` pytest marker was never registered *or* used anywhere — confirmed by grepping
  the whole repo and checking every ollama-adjacent test, all of which already use
  `httpx.MockTransport` or pure config-wiring assertions with no live-network dependency, so there is
  nothing left needing that marker (moot, not a gap). The Dockerfile `USER` directive genuinely was
  missing — added `groupadd`/`useradd`/`chown -R` plus `USER raasoa`, `PYTHONDONTWRITEBYTECODE=1`, and
  an explicit `HOME`. **Verified independently:** built the image, confirmed `whoami`/`id` report
  `uid=999(raasoa)` (non-root); ran the full DB-wait → migrate → serve flow against the real Postgres
  container end-to-end and confirmed `/health/ready` returns `{"ready":true}` — the user switch
  didn't break write access anywhere in the startup path.
  **Also found and fixed one bug from F-046 not on T-28's explicit list:** frontmatter YAML
  block-style lists (`tags:\n  - foo\n  - bar`) were being silently dropped entirely — the parser's
  own comment admitted "could be a list — collect indented lines" but the code just did `continue`
  without collecting anything. Fixed `extract_frontmatter()` in `ingestion/parser.py` to collect
  indented `- item` lines into a list, and added flow-style (`[a, b, c]`) list support for symmetry.
  Added 8 new test cases to `tests/test_ingestion/test_frontmatter.py` (14 total, all passing).
  **Consciously deferred, documented rather than silently dropped** (the other two items named under
  F-046 but not in T-28's action list): (1) *SharePoint per-drive limit starvation* — the sync loop
  gives the first drive in iteration order the full remaining `limit` budget before moving to the
  next, so a single large/busy drive can starve every other drive indefinitely across repeated syncs.
  This is a throughput/fairness issue, not data loss or corruption — eventually correct if the first
  drive's backlog is finite — and a proper fix requires a policy decision (round-robin ordering
  persisted across syncs vs. proportional budget-splitting) that trades off differently depending on
  expected drive sizes; better made with real usage data than guessed here. (2) *`idempotency_key`
  unused* — the field is accepted in `WebhookPayload` but never read. In practice this does **not**
  cause duplicate processing: `ingest_file()` already dedupes on `(tenant_id, source_id,
  source_object_id)` plus a content hash, and a retry with unchanged content short-circuits into a
  cheap metadata-only update rather than a fresh ingest. Wiring the field in for real would need a new
  schema column (or table) plus a TTL/cleanup policy for old idempotency records — a small scoped
  feature, not a one-line fix, so it's flagged here rather than half-implemented.

**Verification:** T-23's and T-25's test suites were run and, via `git stash`, confirmed to fail
without their fixes. T-24 (dashboard XSS) was verified **live in a browser** — `preview_start` against
the real dev server, injecting HTML/script/`javascript:`-URL payloads through mocked API responses on
all four affected pages, confirming via `preview_eval`/`preview_screenshot` that nothing executed and
everything rendered as literal escaped text — not just a static diff review. T-26/T-27's Docker/CI
claims were independently re-verified rather than trusted at face value: a real `docker buildx build`
was run (not assumed from the fixing agent's report), the built image was run against the real
Postgres container end-to-end (DB-wait → masked-DSN log line → migrations → non-root `whoami` →
`/health/ready`), and `mypy`/`ruff` were both re-run locally before accepting the CI hard-gate change.
Full suite after all of Phase D+E plus the additional fixes found during verification (Dockerfile
`USER`, frontmatter list parsing, the 3 additional double-quoted-default columns): **414 passed**, 0
failed, `ruff` and `mypy` both clean.

---

## 5. Open Questions for the Owner — answered 2026-07-06

1. **Deployment model** — **Decided: single-tenant self-hosted.** Dashboard's hard-coded
   `DEFAULT_TENANT` and `AUTH_ENABLED=false` defaults are acceptable as-is; no T-02/T-03-scope changes
   needed.
2. **Auto-resolution policy (F-013)** — **Decided: run unattended on ingest** (reversing the audit's
   opt-in recommendation). `llm_judge_enabled` now defaults to `true` — see the T-13 note above for
   what that actually does and doesn't change.
3. **HubSpot owner ACL (F-036)** — **Decided: manual, via the Admin API.** Documented in
   `DEPLOYMENT.md` §3.6: an admin mints a personal key with `principal_id: "hubspot:owner:<id>"` per
   person who needs restricted-CRM access. No onboarding-sync feature planned; this is intentionally
   a manual, per-person step.
4. **S3/MinIO (F-042)** — **Decided: remove.** `boto3` was already gone (F-042); the `minio` service,
   `miniodata` volume, and all `S3_*` env vars/config fields removed from `docker-compose.yml`,
   `.env.example`, `.env`, and `config.py`. (`docs/rag-service-konzept.md` still describes S3/artifact
   storage as a *future* architectural direction — that's a separate, larger product decision than
   "is the currently-dead scaffolding safe to delete," and wasn't touched.)
5. **Connector completeness** — **Decided: fix the docs.** README corrected to list the 4 real native
   connectors (Notion, SharePoint, Jira, HubSpot) plus the generic webhook endpoint for everything
   else; the false Confluence/"Custom" claims are gone.
6. **Reranker default** — **Decided: harden ollama/cohere into a real supported option** (not change
   the shipped default away from `passthrough`). T-12's correctness fixes (dataclasses.replace,
   SCORE_SCALE) were already done in Phase C; the remaining gap — `CohereRerankProvider.rerank()` had
   zero error handling, so a Cohere outage would 500 `/v1/retrieve`/`/v1/answer` — is closed alongside
   this answer. See T-33 below.

---

## 6. Phase F — Post-audit deployment-readiness fixes — ✅ DONE (2026-07-06)

Triggered by a deployment-readiness review (independent of the T-01–T-28 scope): re-checked every
item in §3 "Needs Verification" live rather than trusting the original audit-time snapshot, which
surfaced 3 new real gaps, then implemented the answers to all 6 §5 open questions plus the 2
previously-deferred F-046 items (SharePoint sync fairness, webhook idempotency).

- **T-29 (found during §3 re-verification)** — `DEPLOYMENT.md` referenced
  `ghcr.io/timtogram/raasoa:latest` and a `make release` target, neither of which exist (CI builds
  with `push: false`; there is no `Makefile` in the repo at all). ✅ §3.1 rewritten to describe the
  only real path (`docker buildx build ... --load .`), cross-referencing §9's already-correct
  build/export/optional-push commands instead of duplicating a broken shortcut.
- **T-30 (found during §3 re-verification)** — `docker-entrypoint.sh`'s runtime `uv run alembic`/
  `uv run uvicorn` calls didn't pass `--frozen --no-sync`, risking a network dependency at container
  startup in network-isolated deployments (the exact scenario §3 flagged: `docker run --network none`).
  ✅ Added to all three runtime `uv run` invocations. **Verified live**, not just by reading the flag
  docs: built the image, stood up an isolated Docker network with only a fresh Postgres reachable (no
  internet, no DNS beyond that one container), and ran the full default entrypoint flow — DB-wait,
  all migrations from scratch, server start, `/health/ready` 200 — successfully with zero network
  access beyond Postgres.
- **T-31 (found during §3 re-verification)** — the HubSpot owner→principal mapping (§5 question 3)
  was confirmed still entirely unwired: `grep -rn "hubspot:owner" src/` shows the ACL grant side is
  real, but nothing ever maps a logged-in person to that principal. ✅ Documented as `DEPLOYMENT.md`
  §3.6 (the owner's chosen manual/admin-API approach). **Verified live**: ran the exact documented
  `POST /v1/admin/enable` → `POST /v1/admin/keys` sequence against a running instance and confirmed
  the response contains the expected `principal_id`.
- **T-32 (§5 questions 1, 4, 5)** — Deployment model: no code change needed (single-tenant confirmed
  fine as shipped). S3/MinIO: removed the `minio` service, `miniodata` volume, and all `S3_*`
  variables from `docker-compose.yml`, `.env.example`, `.env`, and `config.py` — none were ever read
  by application code (`boto3` was already gone). Connector docs: README corrected to the 4 real
  native connectors (Notion, SharePoint, Jira, HubSpot) + generic webhook, removing the false
  Confluence/"Custom" claims. **Verified**: full test suite green after the S3/MinIO removal
  (confirmed a stray real `.env` — gitignored, not just `.env.example` — still had the now-removed
  `S3_*` keys, which made `pydantic-settings`' `extra="forbid"` default hard-fail at import; fixed
  that file too, not just the example).
- **T-33 (§5 question 6)** — `CrossEncoderReranker.rerank()` now catches any exception from the
  underlying `RerankProvider` (covers `CohereRerankProvider`'s total lack of error handling, and any
  future provider) and degrades to the unreranked hybrid-search results with a neutral `0.5` score
  (not the original ~0.033-scale RRF score, which would have silently miscalibrated
  `compute_confidence()` under the reranker's declared `SCORE_SCALE=1.0`) instead of 500ing
  `/v1/retrieve`/`/v1/answer`. `OllamaReranker` already degraded per-item on failure but only logged
  at `debug`; it now also logs one aggregate `warning` per call when any items failed, so a total
  outage is actually visible in production logs. **Verified**: 2 new tests, each confirmed via `git
  stash` to fail against the pre-fix code (one with the exact pre-fix `httpx.ConnectError`
  propagating uncaught, one with the missing aggregate log line).
- **T-34 (F-046, previously deferred)** — SharePoint multi-drive sync fairness. A new pure
  `_rotate_drives()` helper rotates which drive gets first-in-line budget priority each delta-sync
  call (persisted via a `__last_first_drive__` marker reusing the existing per-drive cursor JSON
  blob), guaranteeing every drive gets to go first at least once every `len(drives)` calls instead of
  API-return order permanently starving non-first drives. Cursor persistence changed from
  all-or-nothing (thrown away entirely if the limit was hit before every drive finished — the
  overwhelmingly common case) to progressive: each drive's advanced cursor is written immediately
  after that drive completes. **Verified**: 8 unit tests for `_rotate_drives` (including a full-cycle
  test proving every drive goes first exactly once per `len(drives)` calls) plus one end-to-end test
  driving the real `_sync_sharepoint` orchestration with the Graph API boundary mocked (OAuth, drive
  listing, per-drive delta sync) and real Postgres cursor persistence — proving two consecutive calls
  with a limit too small for one drive to finish reach drive B on the second call, where the old fixed
  order never would. Confirmed via `git stash` that the whole test module fails to even import without
  `_rotate_drives` (the bluntest possible regression signal, but a valid one).
- **T-35 (F-046, previously deferred)** — webhook idempotency-key dedup. New `webhook_idempotency_keys`
  table (migration `r8e9f0a1b2c3`) caches a terminal response per `(tenant_id, idempotency_key)`; a
  retried delivery with the same key short-circuits to the cached response before any processing,
  including source lookup/creation. Deliberately does NOT cache the create/update path's `except
  Exception` branch — a transient failure must remain retryable with the same key. 48h TTL purge added
  to `run_retention_cleanup()`. **Verified**: 5 tests (duplicate-create dedup checked against actual
  document count in the DB, not just the HTTP response; no-key regression check; delete-retry proven
  via a mock call-count assertion that the cascade-delete helper only runs once; a simulated 500
  followed by a real retry with the same key proven to actually succeed; TTL purge proven by inserting
  a backdated row and asserting it's gone after cleanup). 2 of 5 confirmed via `git stash` to fail
  without the fix; the other 3 pass either way by design (they assert no-regression behavior, not the
  new mechanism itself).
- **A critical latent bug surfaced by T-32's `llm_judge_enabled=true` flip** — see the T-13 note
  above for the full `MissingGreenlet` story and the `pipeline.py` fix
  (`await session.refresh(doc)`). Caught only because the full suite was re-run after the config
  change instead of trusting it in isolation; would otherwise have shipped a config flip that 500s
  most real ingests.

**Note on tooling**: the first attempt at T-34/T-35 used a 2-agent parallel `Workflow` run
(`wf_53c28ba7-324`) with very detailed, fully-specified prompts. Both agents stalled after 6 retry
attempts each (~105 minutes, ~308k tokens, zero net progress beyond one already-correct-but-unapplied
migration file for T-35). Root cause not conclusively diagnosed; abandoned rather than retried, and
both tasks were implemented directly instead — a pragmatic recovery given the design was already fully
worked out, not a general judgment against the tool.

**Full verification**: 427 tests passed (up from 414 at the end of Phase E), 0 skipped beyond the
pre-existing DB-reachability skips, `ruff` and `mypy` both clean. Live re-verification: the full
Docker build + network-isolated startup flow (T-30), the documented HubSpot admin-key flow (T-31),
and the full demo (load data → search → conflicts) via the browser preview tools.

---

## 7. Phase G — User-reported issues from live dashboard use — ✅ DONE (2026-07-06)

Triggered by the owner testing the actual dashboard (not the audit process): a screenshot of the
Sources page and a search result that didn't look right.

- **T-36** — `DELETE /v1/sources/{id}` had no ON DELETE rule on `documents.source_id`; deleting a
  source with any documents crashed with an unhandled `IntegrityError` (bare 500). Fixed with an
  explicit document-count check (400 with a clear message) plus a defensive catch for the residual
  race window. The dashboard also had zero delete/edit affordances for sources despite the backend
  endpoint existing — added a Delete button per source card. **Verified live**: clicking Delete on a
  source with documents shows the rejection message and leaves the source intact; deleting an
  empty source actually removes it.
- **T-37** — `compute_confidence()`'s diversity bonus counted raw document count in the result set
  regardless of score coherence, letting a handful of weak, unrelated tail matches inflate confidence
  via sheer count alone — exactly when retrieval found nothing good. Now only documents scoring
  within 70% of the top result count toward the bonus.
- **T-38** — 3 of the 5 "Try these examples" queries on the search page referenced content that
  doesn't exist anywhere in the demo dataset (data visualization tools, remote work policy, cloud
  costs), making retrieval look broken for anyone who clicked them. Replaced with 3 verified-working
  queries, one of which showcases the conflict-detection feature (the 2024 vs. 2026 travel policy
  versions).
- **Root cause of the specific reported bad result**: not a retrieval bug at all — a single stray
  `retrieval_feedback` row left over from testing earlier in this session (`query_text: "Hotel"`) was
  giving one chunk a small but decisive boost across *all* queries, not just Hotel-related ones,
  since query-specific feedback scoping was never implemented (a known, documented Phase C
  trade-off). Deleted the stray row; also did a full cleanup pass removing ~24 accumulated test
  sources/documents from the shared dev database that had built up over this session's testing,
  keeping only the real demo dataset (4 documents under one `File Upload` source).

**Full verification**: 435 tests passed, `ruff` and `mypy` both clean. Live re-verification via
browser preview tools: source deletion (both the rejection and success paths), and the 2 new example
queries retrieving topically correct content.

---

## 8. Deep-dive: does RRF/embedding confidence have a reliable "nothing matches" floor?

Follow-up to Phase G's T-37 fix, which explicitly stopped short of claiming to solve the deeper
issue: RRF-based confidence has no absolute-relevance floor, so "no matching document exists" and "a
moderately-matching document exists" can look statistically similar. Investigated empirically instead
of guessing at a threshold.

**Measurement 1 — raw cosine similarity, real embedding model, real demo corpus** (`ollama` /
`nomic-embed-text`, 768-dim, live Ollama, live pgvector): embedded several genuinely relevant queries
and several genuinely irrelevant ones (including nonsense controls — "beste Pizza-Sorte in Neapel",
"Wetter morgen in Berlin") and measured top-1 cosine similarity against all 4 real demo chunks.

**Result: no reliable separation exists.** A completely unrelated control query ("Pizza in Neapel",
sim=0.666) scored a *higher* top similarity than a genuinely relevant, on-topic query
("Verpflegungspauschale", sim=0.639). "Cloud-Kosten" (irrelevant) scored 0.706, also higher than the
relevant query. Only one irrelevant query ("remote work policy", a short English phrase) showed
markedly lower similarity (~0.28–0.34) — not a general pattern, likely specific to that phrase's
structure. **Conclusion: an absolute cosine-similarity threshold would misclassify at least as often
as it helps with this embedding model on this corpus — not implemented, since there is no principled
threshold to pick.** This is consistent with known anisotropy/isotropy issues in smaller embedding
models: cosine similarity between arbitrary text pairs is compressed into a narrower band than the
"semantically meaningful similarity" intuition assumes.

**Measurement 2 — LLM-based reranker (`OllamaReranker`, qwen3:8b) relevance judgments** on the exact
same query/chunk pairs: **clean separation** — 1.000 for the genuinely relevant pair, 0.000 for every
irrelevant pair (relevant query vs. wrong-topic chunk, and every nonsense-control query vs. either
chunk). Night-and-day difference from raw cosine similarity.

**A real, independent bug was found and fixed while running this measurement**: the *first* run of
this test returned 0.500 (the failure fallback) for every single pair — `OllamaReranker` was
completely non-functional with `qwen3:8b`, silently returning neutral scores 100% of the time. Root
cause: `qwen3` is a reasoning model that emits an internal `<think>...</think>` block before its
answer; the code relied on a `"/no_think"` prompt-prefix hack to suppress this, but the Ollama version
in use returns reasoning in a separate top-level `"thinking"` JSON field rather than inlining it into
`"response"` — so thinking was never suppressed and silently consumed the entire `num_predict=16`
token budget, leaving `"response"` empty on every call. This exact failure mode (a 200 response with
no parseable number) also wasn't counted by the aggregate-failure warning added in the prior
outage-hardening pass, since it isn't an exception. **Fixed**: pass Ollama's documented `"think":
false` API field instead of the prompt hack, and count an unparseable response as a tracked failure.
Verified live (response goes from `""` using all 16 tokens on invisible reasoning, to a directly
parseable `"0"` using 2) and with 2 new regression tests, both confirmed via `git stash` to fail
against the pre-fix code.

**Recommendation for the owner:** a genuine absolute-relevance floor for the answerability gate is
achievable, but requires the LLM-based reranker to actually run on every query (or at least on the
top-1 result, as a cheap "is this really relevant" sanity check before answering) — raw
embedding/RRF confidence cannot do this reliably with the current embedding model, no matter how the
diversity-bonus or normalization math is tuned. This is a real product/cost/latency tradeoff, not a
bug fix, so it wasn't implemented unprompted:

- **Option A — gate only, keep ranking as-is.** Keep `passthrough` as the reranker for ordering
  results (fast, cheap), but always run one extra reranker call on the top-1 result specifically to
  override `answerable` if the LLM judges it irrelevant. Adds one LLM call's latency to every query
  (not per-chunk), smallest behavior change.
- **Option B — make `ollama` the default reranker.** Every query gets full LLM-based reranking, not
  just an answerability sanity check. Better ranking quality across the board, but adds one LLM call
  *per candidate chunk* per query (real latency/cost increase, and depends on the configured chat
  model being pulled and reasonably fast on the deployment's hardware).
- **Option C — leave as a documented, known limitation.** The diversity-bonus fix (T-37) already
  closed the most direct inflation path; a full relevance floor is a larger change that may not be
  worth the added latency/infra dependency until real usage data shows it's actually a problem in
  practice.

No code changes made for this section beyond the reranker bug fix, which stands on its own regardless
of which option (if any) is chosen.
