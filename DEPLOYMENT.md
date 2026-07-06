# RAASOA — Deployment Guide

End-to-end recipe for running RAASOA in production on a Linux server
(Plesk, bare Debian/Ubuntu, or any Docker-capable host).

The recipe targets `linux/amd64` — the same platform our images are
built for.

---

## 1. Prerequisites

| | Minimum | Recommended |
|---|---|---|
| OS | Linux x86_64 with kernel ≥ 5.4 | Debian 12 / Ubuntu 22.04 LTS |
| RAM | 8 GB | 16 GB (more for larger Ollama models) |
| Disk | 20 GB | 100 GB SSD (for pgvector + chunks + ollama cache) |
| Docker | 24.x | 27.x |
| Compose | v2.20+ | latest |

The default stack runs **everything in containers** — no host Python,
no host Postgres. Ollama is the largest single component (~5 GB on
disk for `qwen3:8b`).

### ⚠️ LLM memory requirement (read this before sizing the host)

The chat model powers **claim extraction, contradiction detection and
the LLM Judge** — i.e. the entire governance layer. It must be loaded
into RAM at query time:

| Model | Disk | RAM to load | Use when |
|---|---|---|---|
| `nomic-embed-text` (embeddings) | ~0.3 GB | ~0.5 GB | always (search) |
| `qwen3:8b` (chat, **default**) | ~5 GB | **~6 GB** | host has ≥ 16 GB |
| `qwen3:4b` (chat, lighter) | ~2.5 GB | ~3.5 GB | host has 8–12 GB |
| remote OpenAI-compatible | — | 0 (offloaded) | small host / managed LLM |

If the chat model can't fit, **embeddings and search keep working**,
but ingest logs `model requires more system memory (… GiB) than is
available` and **0 claims are extracted**. Size the host so the chat
model's RAM column fits *alongside* Postgres (~0.5 GB), the API
(~0.5 GB) and OS headroom. The `8 GB` minimum below assumes
`qwen3:4b`; the default `qwen3:8b` needs the `16 GB` tier.

To switch the chat model, set `OLLAMA_CHAT_MODEL` in `.env`. To offload
it entirely, point `EMBEDDING_PROVIDER`/LLM at a remote
OpenAI-compatible endpoint (see `.env.example`).

> Note on CPU-only hosts: qwen3 inference without a GPU is slow
> (seconds–minutes per document). For production ingest throughput,
> use a GPU host or a remote LLM for the chat path.

---

## 2. Production stack overview

```
                ┌──────────────────────────────────────────┐
                │  docker-compose.yml                       │
                │                                           │
   ┌────────┐   │  ┌─────────┐   ┌────────────────────┐    │
   │ client │──▶│  │   api   │──▶│  postgres+pgvector │    │
   └────────┘   │  └─────────┘   └────────────────────┘    │
                │       ▲                                   │
                │       │                                   │
                │  ┌─────────┐   ┌────────────────────┐    │
                │  │scheduler│──▶│      ollama        │    │
                │  └─────────┘   └────────────────────┘    │
                └──────────────────────────────────────────┘
```

- **api** — FastAPI on port 8000. Stateless, scales horizontally.
- **scheduler** — single instance. Runs source-sync, job-queue drain,
  and retention purge loops. Don't run more than one.
- **postgres** — pgvector-enabled. Owns *all* state.
- **ollama** — embedding model + chat (Judge / Claim Extractor) model.

The MCP server (`python -m raasoa.mcp.server`) is **not** part of the
container stack — it runs on the operator's machine (Claude Desktop,
Cursor, …) and talks to the API over HTTPS.

---

## 3. First-time install

### 3.1 Build the image

CI does not publish images anywhere (`.github/workflows/ci.yml` builds with
`push: false` — there is no `ghcr.io/timtogram/raasoa` image to pull).
Build it yourself:

```bash
docker buildx build --platform linux/amd64 -t raasoa:latest --load .
```

To move the image to another machine without a registry, export/import a
tarball (see §9 "Releasing a new version" below for the exact
`docker save`/`docker load` commands) instead of relying on a registry
pull.

### 3.2 Configure secrets

```bash
cp .env.example .env
$EDITOR .env
```

Minimum production overrides (no defaults — set explicitly):

```bash
POSTGRES_PASSWORD=<generated>
DASHBOARD_PASSWORD=<generated>           # leave empty only on private networks
WEBHOOK_SECRET=<32+ chars random>        # required if connectors are enabled
RAASOA_MCP_DEFAULT_CLEARANCE=internal    # tighten if every MCP client is trusted
AUTH_ENABLED=true                        # use API keys, not open access
```

> **Never commit `.env`.** It is already in `.gitignore`.

### 3.3 Start the stack

```bash
docker compose up -d
docker compose logs -f api
```

Migrations run automatically on the first boot. Healthy state looks
like:

```
api-1        | INFO  Application startup complete.
scheduler-1  | INFO  scheduler[sync] tick ok ...
postgres-1   | LOG:  database system is ready to accept connections
```

### 3.4 First tenant + API key

The default tenant `00000000-0000-0000-0000-000000000001` is created
on first ingest, but production deployments should provision proper
tenants:

```bash
curl -X POST https://your-host/v1/tenants \
  -H 'Content-Type: application/json' \
  -d '{"name": "Acme Corp", "plan": "free"}'
```

> Self-service signup (`POST /v1/tenants`) only accepts `"plan":
> "free"` — the endpoint returns `400` for any other value ("Self-service
> signup only supports 'free' plan. Contact sales for paid plans.", see
> `src/raasoa/api/tenants.py`). To provision a `starter`/`pro`/`enterprise`/
> `internal` tenant, insert it directly via SQL (below) and set the plan
> there.

The response contains the new tenant's UUID and a freshly-minted API
key. Hand the key to the consuming application via `Authorization:
Bearer <key>`.

If `SIGNUP_ENABLED=false`, create the tenant directly in the database:

```sql
INSERT INTO tenants (id, name, plan, retention_days, hard_delete_enabled)
VALUES (gen_random_uuid(), 'Acme Corp', 'internal', 730, true)
RETURNING id;

-- id has a server_default (gen_random_uuid()) so it's optional; it's
-- included here just to echo it in the same RETURNING-free statement.
-- key_prefix has no default and must be supplied explicitly. key_hash
-- must match the hex-string format produced by
-- Python's hashlib.sha256(key.encode()).hexdigest() (see
-- raasoa.middleware.auth._hash_key) — encode(digest(..., 'sha256'), 'hex')
-- returns that same hex string, whereas a raw sha256()/digest() call
-- alone returns bytea and will never match on login.
INSERT INTO api_keys (id, tenant_id, key_hash, key_prefix, name)
VALUES (
    gen_random_uuid(),
    '<tenant-uuid>',
    encode(digest('sk-...', 'sha256'), 'hex'),
    'sk-...',  -- short, non-secret display prefix, e.g. 'sk-abc12...ef34'
    'bootstrap'
);
```

> `digest()` requires the `pgcrypto` extension (`CREATE EXTENSION IF NOT
> EXISTS pgcrypto;`) if it isn't already enabled on your database.

### 3.5 Pull Ollama models

The `ollama-pull` service does this on first boot. To force a refresh:

```bash
docker compose run --rm ollama-pull
```

### 3.6 HubSpot restricted-CRM access — mapping a real person to their owner ID

HubSpot CRM records are ingested with an ACL grant on the synthetic
principal `hubspot:owner:<hubspot-owner-id>` (see `api/sources.py`'s
HubSpot sync), so only whoever holds that principal can see that
record's claims via `/v1/retrieve`, `/v1/answer`, or the MCP tools. There
is **no automatic link** between a real logged-in person and that
principal — nothing maps an email address or login to a HubSpot owner ID
on its own. Without doing the following, the restricted-CRM feature is
correctly locked down but invisible to everyone, including the person
it's meant for.

To grant a specific person access to their own HubSpot-owned records,
mint them a personal API key carrying that principal_id:

```bash
# One-time per tenant: turn on the admin API (requires the tenant's own
# master/legacy key — never a personal key).
curl -X POST https://your-host/v1/admin/enable \
  -H 'Authorization: Bearer <tenant-master-key>'

# Look up the person's numeric HubSpot Owner ID first (HubSpot UI:
# Settings -> Objects -> Activities -> Owners, or GET /crm/v3/owners
# on HubSpot's own API) — it is NOT their RAASOA account or email.
curl -X POST https://your-host/v1/admin/keys \
  -H 'Authorization: Bearer <an-admin-capable-key>' \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "Jane — Sales",
    "principal_id": "hubspot:owner:12345678",
    "clearance": "internal"
  }'
```

The response's `key` field is the personal API key for that person —
hand it to them (or wire it into whatever client they use) the same way
as any other API key. There is currently no bulk/onboarding-sync path;
each person who needs restricted-CRM access needs one of these calls
made for them individually as they join.

---

## 4. Operations runbook

### 4.1 Healthchecks

| Endpoint | What it tells you |
|---|---|
| `GET /health` | DB, pgvector version, Ollama model availability |
| `GET /health/ready` | "Ready to accept traffic" (200 = yes) |
| `GET /metrics` | Prometheus counters/gauges (no auth) |

### 4.2 Backups

All state lives in the `pgdata` volume — Postgres is the only stateful
service.

**Hot backup** (no downtime):

```bash
docker exec raasoa-postgres-1 \
  pg_dump -U raasoa -Fc raasoa > raasoa-$(date +%F).dump
gzip raasoa-$(date +%F).dump
```

**Restore** to a fresh volume:

```bash
docker compose down
docker volume rm raasoa_pgdata
docker compose up -d postgres
gunzip -c raasoa-YYYY-MM-DD.dump.gz | \
  docker exec -i raasoa-postgres-1 pg_restore -U raasoa -d raasoa --clean
docker compose up -d
```

Schedule a daily `cron` entry on the host:

```cron
30 2 * * *  docker exec raasoa-postgres-1 pg_dump -U raasoa -Fc raasoa | gzip > /backups/raasoa-$(date +\%F).dump.gz
0  3 * * *  find /backups -name 'raasoa-*.dump.gz' -mtime +30 -delete
```

### 4.3 Migrations

Every container start runs `alembic upgrade head` automatically (see
`docker-entrypoint.sh`). To run them manually:

```bash
docker compose run --rm api alembic upgrade head
docker compose run --rm api alembic current
docker compose run --rm api alembic history --verbose | head -40
```

### 4.4 Retention / GDPR purge

Soft-deleted documents are purged by the scheduler when:
- The owning tenant has `hard_delete_enabled=true`, **and**
- `documents.created_at < now() - tenant.retention_days`.

Run an ad-hoc purge:

> The `scheduler` service pins a hard `entrypoint:` in
> `docker-compose.yml` (`uv run python -m raasoa.worker.scheduler`), so
> a plain trailing command is *appended* to that entrypoint instead of
> replacing it. You must override the entrypoint explicitly:

```bash
docker compose run --rm --entrypoint "uv run python -m raasoa.worker.retention" scheduler
```

### 4.5 Rollback

Tags are immutable (`raasoa:YYYYMMDD`):

```bash
# Pin the previous tag in docker-compose.yml or .env, then:
docker compose pull
docker compose up -d
```

Database rollback should be avoided. If unavoidable:

```bash
docker compose run --rm api alembic downgrade -1
```

`alembic downgrade` works for our schema but **not** for the GDPR
purge (`worker.retention`) — once data is hard-deleted, it's gone.

---

## 5. Scaling

### 5.1 Horizontal API

API workers are stateless. Either bump `UVICORN_WORKERS` (single
container, multiple processes) or run multiple API services behind a
load balancer:

```yaml
api:
  deploy:
    replicas: 3
```

The scheduler **must remain a singleton** (it claims jobs from the
queue). Use `deploy.placement.max_replicas_per_node: 1` if you run
on Swarm/Kubernetes.

### 5.2 Database

Postgres comfortably handles ~10M chunks on a 4-vCPU host. Beyond:

- Move pgvector to a dedicated tier (RDS, AlloyDB, Crunchy).
- Add read replicas; the API uses async sessions and tolerates a
  small staleness window.
- Tune `shared_buffers`, `work_mem`, `effective_cache_size` per the
  pgvector docs.

The `idx_claims_*` and `ix_claims_predicate_trgm` indexes (added in
migration `l2e3f4a5b6c7`) keep the dependency-graph query off
sequential scans up to ~1M claims.

---

## 6. Security checklist

- [ ] `AUTH_ENABLED=true`
- [ ] `DASHBOARD_PASSWORD` set (or dashboard disabled)
- [ ] `WEBHOOK_SECRET` set
- [ ] `RAASOA_MCP_DEFAULT_CLEARANCE` ≥ `internal`
- [ ] TLS terminates at reverse proxy (Plesk / Caddy / Traefik)
- [ ] `CORS_ORIGINS` set to known frontends only
- [ ] Postgres only listens on the docker network (no host port in
      `docker-compose.yml`)
- [ ] Backups encrypted at rest
- [ ] Ollama models pulled from trusted registries

---

## 7. Common issues

| Symptom | Likely cause | Fix |
|---|---|---|
| `health/ready` 503, "model not available" | Ollama-pull race | `docker compose run --rm ollama-pull` |
| Ingest OK but **0 claims**, logs show `model requires more system memory` | Host RAM too small for the chat model | Switch `OLLAMA_CHAT_MODEL=qwen3:4b`, add RAM, or use a remote LLM (see §1) |
| Ollama container stuck `unhealthy`, api never starts | Old healthcheck used `curl` (absent in the image) | Fixed in compose (`ollama list`); pull latest `docker-compose.yml` |
| Scheduler shows `unhealthy` but logs tick fine | It inherited the API HTTP healthcheck | Fixed in compose (`healthcheck.disable`); harmless if you see it on an old file |
| `UndefinedColumnError` after upgrade | Migration skipped on a fork | `docker compose run --rm api alembic upgrade head` |
| Scheduler logs `tick failed` repeatedly | Postgres credentials drift | Recreate `.env`, `docker compose up -d` |
| Search returns nothing | Embedding dimension mismatch | Check `EMBEDDING_DIMENSIONS` matches the model |
| `pgvector extension` errors | Wrong pg image | Use `pgvector/pgvector:pg16`, never plain `postgres:16` |
| MCP server returns 500 from `tools/call` | API not reachable from client | Set `RAASOA_URL=http://your-host` in the MCP env |

---

## 8. Plesk-specific notes

Plesk Docker UI:

1. Import the offline image (`raasoa-YYYYMMDD.tar.gz`) via *Tools →
   Docker → Add Image*.
2. Create a network `raasoa-net`. Attach the postgres, api, scheduler
   containers to it.
3. Map host port `8000` → container port `8000` for `api` only.
   Postgres should not be exposed to the host.
4. In *Apache & nginx Settings* of your domain add:
   ```
   location / { proxy_pass http://api:8000; }
   ```
5. Set environment variables in Plesk's *Environment* tab — they map
   1:1 to the `.env` keys.

---

## 9. Releasing a new version

```bash
# Build and tag
docker buildx build --platform linux/amd64 \
  -t raasoa:$(date +%Y%m%d) -t raasoa:latest --load .

# Export for offline transfer
docker save raasoa:$(date +%Y%m%d) | gzip > dist/raasoa-$(date +%Y%m%d).tar.gz

# (optional) Push to a registry
docker tag raasoa:latest ghcr.io/timtogram/raasoa:latest
docker push ghcr.io/timtogram/raasoa:latest
```

The tag matches the convention required by the team's [global
guidelines](https://github.com/timtogram/raasoa/blob/main/CONTRIBUTING.md):
`{project}:{YYYYMMDD}` plus `latest`.
