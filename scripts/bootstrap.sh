#!/usr/bin/env bash
# FruitcakeAI pre-alpha bootstrap — canonical alpha backend startup path.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# shellcheck disable=SC1091
source "$ROOT/scripts/_install_common.sh"

print_header "Checking required commands"
ensure_command docker "Install Docker Desktop and start it, then rerun ./scripts/bootstrap.sh"
ensure_command ollama "Install Ollama, then rerun ./scripts/bootstrap.sh"
ensure_command python3.11 "Install Python 3.11, then rerun ./scripts/bootstrap.sh"
ensure_command curl "Install curl, then rerun ./scripts/bootstrap.sh"

print_header "Checking Docker daemon"
if ! docker_daemon_ready; then
  echo "ERROR: Docker is installed but the daemon is not reachable."
  echo "Next step: start Docker Desktop and rerun ./scripts/bootstrap.sh"
  exit 1
fi

print_header "Preparing environment"
ensure_env_file
load_env_file

print_header "Checking Ollama"
if ! ollama_ready; then
  start_ollama_if_needed
fi

print_header "Preparing Python environment"
ensure_venv

print_header "Ensuring required local models"
ensure_models

print_header "Starting PostgreSQL"
start_postgres

print_header "Waiting for database readiness"
wait_for_postgres
echo "  Database ready."

print_header "Running migrations"
run_migrations

print_header "Seeding default users"
seed_default_users

print_header "Starting FruitcakeAI API"
echo "  Health check after boot: $(health_url)"
exec uvicorn app.main:app --host 0.0.0.0 --port "$APP_PORT" --reload
