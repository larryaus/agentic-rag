#!/usr/bin/env bash
set -e

echo "[entrypoint] running migrations..."
for f in /app/migrations/*.sql; do
  echo "  -> $f"
  psql -v ON_ERROR_STOP=1 "$DATABASE_URL" -f "$f"
done

echo "[entrypoint] seeding sample documents (idempotent)..."
python /app/scripts/seed.py || echo "[entrypoint] seed step skipped (continuing)"

echo "[entrypoint] starting uvicorn..."
exec uvicorn agent_service.main:app --host 0.0.0.0 --port 8000
