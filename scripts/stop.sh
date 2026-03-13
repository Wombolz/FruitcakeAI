#!/usr/bin/env bash
# FruitcakeAI v5 — Stop local services started for development
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

STOP_OLLAMA=false
if [[ "${1:-}" == "--all" ]]; then
  STOP_OLLAMA=true
fi

echo "▶ Stopping FruitcakeAI API processes..."
API_PIDS="$(pgrep -f "uvicorn app.main:app" || true)"
if [[ -n "$API_PIDS" ]]; then
  # shellcheck disable=SC2086
  kill $API_PIDS 2>/dev/null || true
  sleep 1
  STILL_RUNNING="$(pgrep -f "uvicorn app.main:app" || true)"
  if [[ -n "$STILL_RUNNING" ]]; then
    echo "  Forcing API shutdown..."
    # shellcheck disable=SC2086
    kill -9 $STILL_RUNNING 2>/dev/null || true
  fi
  echo "  API stopped."
else
  echo "  No API process found."
fi

echo "▶ Stopping PostgreSQL container..."
docker compose stop postgres >/dev/null 2>&1 || true
echo "  Postgres stopped (or was not running)."

if [[ "$STOP_OLLAMA" == "true" ]]; then
  echo "▶ Stopping Ollama..."
  OLLAMA_PIDS="$(pgrep -f "ollama serve" || true)"
  if [[ -n "$OLLAMA_PIDS" ]]; then
    # shellcheck disable=SC2086
    kill $OLLAMA_PIDS 2>/dev/null || true
    echo "  Ollama stopped."
  else
    echo "  No Ollama process found."
  fi
else
  echo "▶ Leaving Ollama running. Use '--all' to stop it too."
fi

echo "✅ Shutdown complete."
