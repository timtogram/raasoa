#!/bin/bash
set -e

echo "Running database migrations..."
uv run alembic upgrade head

echo "Starting RAASOA API server..."
exec uv run uvicorn raasoa.main:app --host 0.0.0.0 --port 8000
