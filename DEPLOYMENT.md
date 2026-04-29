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
                │                                           │
                │  ┌─────────┐                              │
                │  │  minio  │  (optional artifact store)   │
                │  └─────────┘                              │
                └──────────────────────────────────────────┘
```

- **api** — FastAPI on port 8000. Stateless, scales horizontally.
- **scheduler** — single instance. Runs source-sync, job-queue drain,
  and retention purge loops. Don't run more than one.
- **postgres** — pgvector-enabled. Owns *all* state.
- **ollama** — embedding model + chat (Judge / Claim Extractor) model.
- **minio** — optional. Only used when you keep raw artifacts on S3.

The MCP server (`python -m raasoa.mcp.server`) is **not** part of the
container stack — it runs on the operator's machine (Claude Desktop,
Cursor, …) and talks to the API over HTTPS.

---

## 3. First-time install

### 3.1 Pull the image

If you have GitHub Container Registry access:

```bash
docker pull ghcr.io/timtogram/raasoa:latest
```

Or load the offline tarball produced by `make release`:

```bash
gunzip -c raasoa-YYYYMMDD.tar.gz | docker load
```

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
curl -X POST https://your-host/v1/tenants/signup \
  -H 'Content-Type: application/json' \
  -d '{"name": "Acme Corp", "plan": "internal"}'
```

The response contains the new tenant's UUID and a freshly-minted API
key. Hand the key to the consuming application via `Authorization:
Bearer <key>`.

If `SIGNUP_ENABLED=false`, create the tenant directly in the database:

```sql
INSERT INTO tenants (id, name, plan, retention_days, hard_delete_enabled)
VALUES (gen_random_uuid(), 'Acme Corp', 'internal', 730, true)
RETURNING id;

INSERT INTO api_keys (tenant_id, key_hash, name)
VALUES ('<tenant-uuid>', sha256(decode('sk-...', 'escape')), 'bootstrap');
```

### 3.5 Pull Ollama models

The `ollama-pull` service does this on first boot. To force a refresh:

```bash
docker compose run --rm ollama-pull
```

---

## 4. Operations runbook

### 4.1 Healthchecks

| Endpoint | What it tells you |
|---|---|
| `GET /health` | DB, pgvector version, Ollama model availability |
| `GET /health/ready` | "Ready to accept traffic" (200 = yes) |
| `GET /metrics` | Prometheus counters/gauges (no auth) |

### 4.2 Backups

State lives in two volumes: `pgdata` and (optionally) `miniodata`.

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

```bash
docker compose run --rm scheduler \
  uv run python -m raasoa.worker.retention
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
