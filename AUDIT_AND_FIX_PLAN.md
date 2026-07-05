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
have no defaults (root of the broken bootstrap SQL, F-035).

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
  never a "contradiction"). Additionally, `settings.llm_judge_enabled` now defaults to `false`
  (`config.py`, `.env.example`, both `docker-compose.yml` service blocks) — unattended, permanent
  claim supersession now requires an explicit opt-in. Verified this doesn't break the demo: `/v1/answer`
  still correctly cites the newer document via RAG ranking regardless of claim-supersession state
  (confirmed live before and after), and the demo's meal-allowance conflict now stays visible under
  `/v1/conflicts` for the user to review, matching the README's own description of the flow.
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

**Not yet done:** Phases D–E (T-19 through T-28) and the Open Questions in §5.

### Phase D — Connectors & worker

- **T-19 (F-022)** — `api/sources.py`. Paginate Notion search (`next_cursor`/`has_more`); set the
  delta cursor from the max `last_edited_time` seen, not wall clock; normalize timestamp formats.
  **Accept:** a >100-page workspace ingests all pages across runs. **M / medium.**
- **T-20 (F-010, F-025)** — `api/sources.py`, `api/webhooks.py`, `worker/retention.py`. On owner
  reassignment delete the *old* owner grant; on delete cascade to (or filter) chunks/claims/acl_entries/crm_objects.
  **Accept:** reassigned HubSpot record drops the old owner's access; deleted doc's ACL rows are gone.
  **M / medium.**
- **T-21 (F-023, F-024)** — `ingestion/tiering.py`, `worker/queue.py`. Bind the interval param
  correctly (`now() - (:cold_days || ' days')::interval`); honor `max_attempts` before marking
  `failed`. **Accept:** `run_tiering_sweep` executes without interval syntax error; a failing job
  retries up to `max_attempts`. **S / low.**
- **T-22 (F-031, F-032)** — `quality/conflicts.py`, `duplicate.py`, `ingestion/pipeline.py`. Filter
  `status != 'deleted'` in conflict/dup queries; make the index update incremental per-document (or
  lock) instead of a full tenant rebuild. **Accept:** re-uploading a deleted doc isn't flagged as its
  own duplicate; concurrent ingests don't corrupt the index. **M / medium.**

### Phase E — Data hygiene, deploy, docs

- **T-23 (F-026)** — `api/ingestion.py`. Enforce size limit via streaming / `Content-Length` before
  reading the whole body. **Accept:** oversized upload rejected without buffering it all. **S / low.**
- **T-24 (F-027, F-028)** — `templates/search.html`, `upload.html`, `sources.html`, `account.html`.
  Escape user content in JS `innerHTML` sinks (use `textContent`/escape helper); fix the `\\'`
  escaping in revoke/sync onclick handlers. **Accept:** a doc titled `<img onerror>` doesn't execute;
  Revoke and post-connect Sync buttons work. **M / medium.**
- **T-25 (F-033, F-034, F-037)** — `models/*.py`. Add ORM models/columns for the 11 migration-only
  tables + `admin_api_enabled`/`default_visibility`; make `chunk.embedding` dimension consistent with
  config across model and migration; fix the `plan` server default (`'free'` → `free`). **Accept:**
  `alembic revision --autogenerate` produces an empty diff. **L / medium.**
- **T-26 (F-035, F-003)** — `Dockerfile`, `docker-entrypoint.sh`, `DEPLOYMENT.md`. Copy `examples/`
  into the image (fixes F-003); make the entrypoint pass `"$@"` so `docker compose run api alembic …`
  works; correct the bootstrap-key SQL and the signup path in docs. **Accept:** Load Demo Data works
  in a built image; documented ops commands run as written. **M / medium.**
- **T-27 (F-041, F-042, F-045)** — `docker-entrypoint.sh`, `pyproject.toml`, `.github/workflows/ci.yml`.
  Stop echoing the DSN; remove `boto3`+S3 config (or wire artifact storage); make mypy/format gate CI
  now that both pass. **Accept:** startup logs contain no password; CI fails on a type error. **S / low.**
- **T-28 (F-038, F-039, F-040, F-044, F-046)** — Minor polish batch: generic 500 detail in
  sources.py:552; `Query(ge=…, le=…)` bounds on unbounded list params; `hmac.compare_digest` for the
  webhook secret; rebuild/version-bump the client wheel; reconcile tool-count docs; add a `USER`
  directive to the Dockerfile; register the `requires_ollama` pytest marker. **Accept:** ruff/mypy/tests
  green; params bounded. **S / low.**

---

## 5. Open Questions for the Owner

1. **Deployment model** — single-tenant self-hosted (dashboard's hard-coded `DEFAULT_TENANT` is fine)
   or multi-tenant SaaS (dashboard must become tenant-aware, `AUTH_ENABLED=false` defaults must go)?
   Changes T-02/T-03 scope.
2. **Auto-resolution policy (F-013)** — should contradiction auto-resolution ever run unattended on
   ingest, or always route to human review? Recommendation: opt-in / off-by-default.
3. **HubSpot owner ACL (F-036)** — how is a logged-in user linked to their `hubspot:owner:<id>`?
   Manual membership or an onboarding sync? Without it the restricted-CRM feature hides everything
   from everyone.
4. **S3/MinIO (F-042)** — planned feature (wire it up) or dead scaffolding (remove boto3 + MinIO
   service + config)?
5. **Connector completeness** — README lists Confluence and "Custom"; only a generic webhook exists.
   Advertise-as-webhook-only, or implement?
6. **Reranker default** — `passthrough` is the config default and the confidence math assumes it. Is
   `ollama`/`cohere` reranking a supported prod config (then T-12 is required) or experimental?
