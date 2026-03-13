#!/usr/bin/env bash
# FruitcakeAI v5 — Wipe and reseed the database (development only)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "⚠️  This will DROP and recreate the fruitcake_v5 database."
read -r -p "Continue? [y/N] " confirm
[[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

echo "▶ Stopping services..."
docker compose down

echo "▶ Removing postgres volume..."
docker volume rm fruitcake_v5_pgdata 2>/dev/null || true

echo "▶ Starting fresh postgres..."
docker compose up -d postgres

echo "▶ Waiting for database..."
until docker compose exec postgres pg_isready -U fruitcake -d fruitcake_v5 &>/dev/null; do
  sleep 1
done

echo "▶ Running migrations..."
source .venv/bin/activate
alembic upgrade head

echo "▶ Seeding default users..."
python scripts/seed.py

echo "✅ Database reset complete."
