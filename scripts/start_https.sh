#!/usr/bin/env bash
# FruitcakeAI HTTPS startup helper — loopback backend + Caddy front door.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# shellcheck disable=SC1091
source "$ROOT/scripts/_install_common.sh"

print_header "Checking required commands"
ensure_command caddy "Install Caddy (for example: brew install caddy), then rerun ./scripts/start_https.sh"
ensure_command docker "Install Docker Desktop and start it, then rerun ./scripts/start_https.sh"
ensure_command ollama "Install Ollama, then rerun ./scripts/start_https.sh"
ensure_command python3.11 "Install Python 3.11, then rerun ./scripts/start_https.sh"
ensure_command curl "Install curl, then rerun ./scripts/start_https.sh"

print_header "Checking Docker daemon"
if ! docker_daemon_ready; then
  echo "ERROR: Docker is installed but the daemon is not reachable."
  echo "Next step: start Docker Desktop and rerun ./scripts/start_https.sh"
  exit 1
fi

print_header "Preparing environment"
ensure_env_file
load_env_file

SITE_ADDRESS="${FRUITCAKE_SITE_ADDRESS:-fruitcake.localhost}"
UPSTREAM_ADDRESS="${FRUITCAKE_UPSTREAM:-127.0.0.1:${APP_PORT}}"
CADDY_CONFIG_PATH="${FRUITCAKE_CADDYFILE:-$ROOT/Caddyfile}"

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

api_pid=""
caddy_pid=""

cleanup() {
  local exit_code="$?"
  if [[ -n "$caddy_pid" ]] && kill -0 "$caddy_pid" >/dev/null 2>&1; then
    kill "$caddy_pid" >/dev/null 2>&1 || true
    wait "$caddy_pid" >/dev/null 2>&1 || true
  fi
  if [[ -n "$api_pid" ]] && kill -0 "$api_pid" >/dev/null 2>&1; then
    kill "$api_pid" >/dev/null 2>&1 || true
    wait "$api_pid" >/dev/null 2>&1 || true
  fi
  exit "$exit_code"
}

trap cleanup EXIT INT TERM

print_header "Starting FruitcakeAI API on loopback"
echo "  Backend upstream: http://127.0.0.1:${APP_PORT}"
.venv/bin/uvicorn app.main:app \
  --host 127.0.0.1 \
  --port "$APP_PORT" \
  --proxy-headers \
  --forwarded-allow-ips "127.0.0.1,::1" &
api_pid="$!"

print_header "Waiting for backend health"
for _ in $(seq 1 30); do
  if curl -sf "http://127.0.0.1:${APP_PORT}/health" >/dev/null 2>&1; then
    echo "  Backend ready."
    break
  fi
  sleep 1
done

if ! curl -sf "http://127.0.0.1:${APP_PORT}/health" >/dev/null 2>&1; then
  echo "ERROR: backend did not become healthy on loopback."
  exit 1
fi

print_header "Starting Caddy HTTPS front door"
echo "  Site address: https://${SITE_ADDRESS}"
echo "  Caddy config: ${CADDY_CONFIG_PATH}"
echo "  Upstream: ${UPSTREAM_ADDRESS}"
echo
echo "If you are using the default 'tls internal' config, trust Caddy's local CA once with:"
echo "  sudo caddy trust"
echo
echo "For a public-domain deployment, edit Caddyfile and remove the 'tls internal' line first."
echo

FRUITCAKE_SITE_ADDRESS="$SITE_ADDRESS" \
FRUITCAKE_UPSTREAM="$UPSTREAM_ADDRESS" \
caddy run --config "$CADDY_CONFIG_PATH" --adapter caddyfile &
caddy_pid="$!"

wait "$caddy_pid"
