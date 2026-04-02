#!/bin/bash
set -e

echo "=== RAASOA Startup ==="
echo "Database: ${DATABASE_URL:-not set}"
echo "Embedding: ${EMBEDDING_PROVIDER:-ollama}"

# Wait for database to be ready
echo "Waiting for database..."
for i in $(seq 1 30); do
    if uv run python -c "
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

echo "Running database migrations..."
uv run alembic upgrade head

echo "Starting RAASOA API server..."
exec uv run uvicorn raasoa.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --workers "${UVICORN_WORKERS:-1}" \
    --log-level "${LOG_LEVEL:-info}"
