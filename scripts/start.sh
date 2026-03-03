#!/usr/bin/env bash
# FruitcakeAI v5 — Start everything
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "▶ Starting PostgreSQL..."
docker compose up -d postgres

echo "▶ Waiting for database to be ready..."
until docker compose exec postgres pg_isready -U fruitcake -d fruitcake_v5 &>/dev/null; do
  sleep 1
done
echo "  Database ready."

echo "▶ Checking Ollama..."
if curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
  echo "  Ollama is already running."
else
  echo "  Ollama not running — starting it in the background..."
  ollama serve &>/tmp/ollama.log &
  OLLAMA_PID=$!
  echo "  Waiting for Ollama to be ready (pid $OLLAMA_PID)..."
  for i in $(seq 1 30); do
    if curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
      echo "  Ollama ready."
      break
    fi
    sleep 1
    if [ "$i" -eq 30 ]; then
      echo "  WARNING: Ollama did not start within 30s — check /tmp/ollama.log"
    fi
  done
fi

echo "▶ Running Alembic migrations..."
alembic upgrade head

echo "▶ Seeding default users (skips existing)..."
python scripts/seed.py

echo "▶ Starting FruitcakeAI API..."
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
