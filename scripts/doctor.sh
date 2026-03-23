#!/usr/bin/env bash
# FruitcakeAI pre-alpha diagnostics / preflight command.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# shellcheck disable=SC1091
source "$ROOT/scripts/_install_common.sh"

PASS_COUNT=0
DEGRADED_COUNT=0
FAIL_COUNT=0

pass() {
  PASS_COUNT=$((PASS_COUNT + 1))
  echo "PASS: $1"
}

degraded() {
  DEGRADED_COUNT=$((DEGRADED_COUNT + 1))
  echo "DEGRADED: $1"
  [[ -n "${2:-}" ]] && echo "  Next step: $2"
}

fail() {
  FAIL_COUNT=$((FAIL_COUNT + 1))
  echo "FAIL: $1"
  [[ -n "${2:-}" ]] && echo "  Next step: $2"
}

check_command() {
  local command_name="$1"
  local install_hint="$2"
  if command -v "$command_name" >/dev/null 2>&1; then
    pass "$command_name is available"
  else
    fail "$command_name is missing" "$install_hint"
  fi
}

print_header "FruitcakeAI doctor"
echo "  Root: $ROOT"

check_command docker "Install Docker Desktop and start it"
check_command ollama "Install Ollama"
check_command python3.11 "Install Python 3.11"
check_command curl "Install curl"

if docker_daemon_ready; then
  pass "Docker daemon is reachable"
else
  fail "Docker daemon is not reachable" "Start Docker Desktop"
fi

if [[ -d "$ROOT/.venv" ]]; then
  pass ".venv exists"
else
  fail ".venv is missing" "Run ./scripts/bootstrap.sh"
fi

if [[ -f "$ENV_FILE" ]]; then
  pass ".env exists"
else
  fail ".env is missing" "Run ./scripts/bootstrap.sh"
fi

if ollama_ready; then
  pass "Ollama is reachable at $OLLAMA_BASE_URL"
else
  fail "Ollama is not reachable" "Run 'ollama serve' or rerun ./scripts/bootstrap.sh"
fi

if docker_daemon_ready; then
  if docker compose ps postgres --format json 2>/dev/null | grep -q '"State":"running"'; then
    pass "Postgres container is running"
  else
    fail "Postgres container is not running" "Run ./scripts/bootstrap.sh"
  fi
fi

if [[ -f "$ENV_FILE" ]]; then
  load_env_file
fi

if [[ -d "$ROOT/.venv" ]]; then
  # shellcheck disable=SC1091
  source "$ROOT/.venv/bin/activate"
  if alembic current 2>/dev/null | grep -q '(head)'; then
    pass "Alembic is at head"
  else
    fail "Alembic is not at head" "Run ./scripts/bootstrap.sh or 'alembic upgrade head'"
  fi
fi

if ollama_ready; then
  tags="$(curl -sf "$OLLAMA_BASE_URL/api/tags" || true)"
  while IFS= read -r model; do
    [[ -z "$model" ]] && continue
    if [[ "$tags" == *"\"name\":\"$model\""* ]]; then
      pass "Required Ollama model present: $model"
    else
      fail "Required Ollama model missing: $model" "Run 'ollama pull $model' or rerun ./scripts/bootstrap.sh"
    fi
  done < <(required_model_list "$(ram_tier)")
fi

health_status="$(curl -sf "$(health_url)" 2>/dev/null || true)"
if [[ "$health_status" == *'"status":"ok"'* ]]; then
  pass "Backend /health is healthy"
else
  fail "Backend /health is not healthy or not reachable" "Start the backend with ./scripts/bootstrap.sh"
fi

if [[ "$health_status" == *'"status":"ok"'* ]]; then
  admin_user="${FRUITCAKE_ADMIN_USERNAME:-admin}"
  admin_password="${FRUITCAKE_ADMIN_PASSWORD:-changeme123}"
  token_response="$(curl -sf -X POST "http://localhost:${APP_PORT}/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"username\":\"${admin_user}\",\"password\":\"${admin_password}\"}" 2>/dev/null || true)"
  admin_token="$(printf '%s' "$token_response" | sed -n 's/.*"access_token":"\([^"]*\)".*/\1/p')"
  if [[ -n "$admin_token" ]]; then
    admin_health="$(curl -sf "http://localhost:${APP_PORT}/admin/health" \
      -H "Authorization: Bearer ${admin_token}" 2>/dev/null || true)"
    if [[ "$admin_health" == *'"status":"ok"'* || "$admin_health" == *'"status":"degraded"'* ]]; then
      pass "Admin /admin/health is reachable"
    else
      degraded "Admin /admin/health did not return a healthy payload" "Log in as an admin user and inspect /admin/health manually"
    fi
  else
    degraded "Admin /admin/health was not checked" "Set FRUITCAKE_ADMIN_USERNAME / FRUITCAKE_ADMIN_PASSWORD if you want doctor to verify the admin health endpoint"
  fi
fi

if docker image inspect fruitcake/mcp-shell >/dev/null 2>&1; then
  pass "Optional shell MCP image is present"
else
  degraded "Optional shell MCP image is missing" "Build it later with 'docker build -t fruitcake/mcp-shell -f mcp_shell_server/Dockerfile .' if you want shell tools"
fi

if [[ -n "${APNS_KEY_ID:-}" && -n "${APNS_TEAM_ID:-}" && -n "${APNS_AUTH_KEY_PATH:-}" ]]; then
  pass "Optional APNs config is present"
else
  degraded "Optional APNs config is not set" "Leave it unset for alpha unless you need push delivery"
fi

if (( FAIL_COUNT > 0 )); then
  overall="FAIL"
elif (( DEGRADED_COUNT > 0 )); then
  overall="DEGRADED"
else
  overall="PASS"
fi

echo
echo "Summary: $overall ($PASS_COUNT pass, $DEGRADED_COUNT degraded, $FAIL_COUNT fail)"
