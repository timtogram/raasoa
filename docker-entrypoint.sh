#!/bin/bash
set -e

echo "=== RAASOA Startup ==="
if [ -n "${DATABASE_URL:-}" ]; then
    echo "Database: configured (${DATABASE_URL##*@})"
else
    echo "Database: not set"
fi
echo "Embedding: ${EMBEDDING_PROVIDER:-ollama}"

# Wait for database to be ready
echo "Waiting for database..."
for i in $(seq 1 30); do
    if uv run --frozen --no-sync python -c "
import asyncio, asyncpg, os
async def check():
    url = os.environ.get('DATABASE_URL', '').replace('+asyncpg', '')
    conn = await asyncpg.connect(url)
    await conn.close()
asyncio.run(check())
" 2>/dev/null; then
        echo "Database ready."
        break
    fi
    echo "  Attempt $i/30 — waiting..."
    sleep 2
done

# If the container was invoked with an explicit command (e.g.
# `docker compose run --rm api alembic upgrade head`), run that instead
# of the default migrate-then-serve flow. The DB-wait loop above still
# runs first since most overrides (alembic, shell, etc.) need the DB too.
if [ "$#" -gt 0 ]; then
    echo "Running passed-through command: $*"
    exec "$@"
fi

echo "Running database migrations..."
uv run --frozen --no-sync alembic upgrade head

echo "Starting RAASOA API server..."
exec uv run --frozen --no-sync uvicorn raasoa.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --workers "${UVICORN_WORKERS:-1}" \
    --log-level "${LOG_LEVEL:-info}"
