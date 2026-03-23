#!/usr/bin/env bash
# Shared install/bootstrap helpers for FruitcakeAI pre-alpha onboarding.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_PORT="${APP_PORT:-30417}"
OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-http://localhost:11434}"
ENV_FILE="$ROOT/.env"
ENV_TEMPLATE="$ROOT/.env.example"
REQUIREMENTS_FILE="$ROOT/requirements.txt"
REQUIREMENTS_STAMP="$ROOT/.venv/.fruitcake-requirements.sha256"

ram_gb() {
  local bytes
  bytes="$(sysctl -n hw.memsize 2>/dev/null || true)"
  if [[ -z "$bytes" ]]; then
    echo "0"
    return
  fi
  awk -v bytes="$bytes" 'BEGIN { printf "%.0f\n", bytes / 1024 / 1024 / 1024 }'
}

ram_tier() {
  local gb
  gb="$(ram_gb)"
  if [[ "$gb" =~ ^[0-9]+$ ]] && (( gb >= 64 )); then
    echo "high"
  else
    echo "standard"
  fi
}

required_model_list() {
  local tier="${1:-$(ram_tier)}"
  if [[ "$tier" == "high" ]]; then
    printf '%s\n' "qwen2.5:14b" "qwen2.5:32b"
  else
    printf '%s\n' "qwen2.5:14b"
  fi
}

ensure_command() {
  local command_name="$1"
  local install_hint="$2"
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "ERROR: '$command_name' is required but not found."
    echo "Next step: $install_hint"
    return 1
  fi
}

docker_daemon_ready() {
  docker info >/dev/null 2>&1
}

ollama_ready() {
  curl -sf "$OLLAMA_BASE_URL/api/tags" >/dev/null 2>&1
}

start_ollama_if_needed() {
  if ollama_ready; then
    return 0
  fi

  echo "▶ Ollama is not reachable — trying to start it..."
  nohup ollama serve >/tmp/fruitcakeai-ollama.log 2>&1 &

  for _ in $(seq 1 30); do
    if ollama_ready; then
      echo "  Ollama ready."
      return 0
    fi
    sleep 1
  done

  echo "WARNING: Ollama did not become ready within 30 seconds."
  echo "Next step: inspect /tmp/fruitcakeai-ollama.log or start Ollama manually with 'ollama serve'"
  return 1
}

ensure_env_file() {
  if [[ -f "$ENV_FILE" ]]; then
    return 0
  fi

  local tier
  tier="$(ram_tier)"
  local total_ram
  total_ram="$(ram_gb)"

  cp "$ENV_TEMPLATE" "$ENV_FILE"

  if [[ "$tier" == "high" ]]; then
    perl -0pi -e 's/LLM_MODEL=ollama_chat\/qwen2\.5:32b/LLM_MODEL=ollama_chat\/qwen2.5:32b/' "$ENV_FILE"
    perl -0pi -e 's/LOCAL_MODEL=qwen2\.5:32b/LOCAL_MODEL=qwen2.5:32b/' "$ENV_FILE"
    perl -0pi -e 's/TASK_SMALL_MODEL=ollama_chat\/qwen2\.5:14b/TASK_SMALL_MODEL=ollama_chat\/qwen2.5:14b/' "$ENV_FILE"
    perl -0pi -e 's/TASK_LARGE_MODEL=ollama_chat\/qwen2\.5:32b/TASK_LARGE_MODEL=ollama_chat\/qwen2.5:32b/' "$ENV_FILE"
  else
    perl -0pi -e 's/LLM_MODEL=ollama_chat\/qwen2\.5:32b/LLM_MODEL=ollama_chat\/qwen2.5:14b/' "$ENV_FILE"
    perl -0pi -e 's/LOCAL_MODEL=qwen2\.5:32b/LOCAL_MODEL=qwen2.5:14b/' "$ENV_FILE"
    perl -0pi -e 's/TASK_SMALL_MODEL=ollama_chat\/qwen2\.5:14b/TASK_SMALL_MODEL=ollama_chat\/qwen2.5:14b/' "$ENV_FILE"
    perl -0pi -e 's/TASK_LARGE_MODEL=ollama_chat\/qwen2\.5:32b/TASK_LARGE_MODEL=ollama_chat\/qwen2.5:14b/' "$ENV_FILE"
  fi

  echo "▶ Created .env from template."
  echo "  Detected RAM: ${total_ram} GB"
  if [[ "$tier" == "high" ]]; then
    echo "  Selected model tier: >=64 GB (14b + 32b mixed routing)"
  else
    echo "  Selected model tier: <64 GB (all 14b defaults)"
  fi
  echo "  Override models later by editing .env if needed."
}

load_env_file() {
  if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
  fi
}

configured_llm_backend() {
  if [[ -f "$ENV_FILE" ]]; then
    local backend
    backend="$(sed -n 's/^LLM_BACKEND=//p' "$ENV_FILE" | head -n 1)"
    if [[ -n "$backend" ]]; then
      printf '%s\n' "$backend"
      return
    fi
  fi
  printf '%s\n' "ollama"
}

configured_ollama_models() {
  if [[ ! -f "$ENV_FILE" ]]; then
    return 1
  fi

  local backend
  backend="$(configured_llm_backend)"
  if [[ "$backend" != "ollama" ]]; then
    return 1
  fi

  local llm_model task_small task_large
  llm_model="$(sed -n 's/^LLM_MODEL=ollama_chat\///p' "$ENV_FILE" | head -n 1)"
  task_small="$(sed -n 's/^TASK_SMALL_MODEL=ollama_chat\///p' "$ENV_FILE" | head -n 1)"
  task_large="$(sed -n 's/^TASK_LARGE_MODEL=ollama_chat\///p' "$ENV_FILE" | head -n 1)"

  [[ -n "$llm_model" ]] && printf '%s\n' "$llm_model"
  [[ -n "$task_small" ]] && printf '%s\n' "$task_small"
  [[ -n "$task_large" ]] && printf '%s\n' "$task_large"
  return 0
}

unique_lines() {
  awk 'NF && !seen[$0]++'
}

ensure_venv() {
  if [[ ! -d "$ROOT/.venv" ]]; then
    python3.11 -m venv "$ROOT/.venv"
  fi
  # shellcheck disable=SC1091
  source "$ROOT/.venv/bin/activate"

  local current_hash existing_hash
  current_hash="$(shasum -a 256 "$REQUIREMENTS_FILE" | awk '{print $1}')"
  existing_hash=""
  if [[ -f "$REQUIREMENTS_STAMP" ]]; then
    existing_hash="$(cat "$REQUIREMENTS_STAMP")"
  fi

  if [[ "$current_hash" != "$existing_hash" ]]; then
    pip install -q -r "$ROOT/requirements.txt"
    printf '%s\n' "$current_hash" > "$REQUIREMENTS_STAMP"
  fi
}

ensure_models() {
  local backend
  backend="$(configured_llm_backend)"
  if [[ "$backend" != "ollama" ]]; then
    echo "  Skipping Ollama model checks because LLM_BACKEND=$backend"
    return 0
  fi

  local models
  if configured_ollama_models >/dev/null 2>&1; then
    mapfile -t models < <(configured_ollama_models | unique_lines)
  else
    mapfile -t models < <(required_model_list "$(ram_tier)")
  fi

  local model
  local tags
  tags="$(curl -sf "$OLLAMA_BASE_URL/api/tags" || true)"
  for model in "${models[@]}"; do
    if [[ "$tags" == *"\"name\":\"$model\""* ]]; then
      echo "  Ollama model present: $model"
    else
      echo "▶ Pulling Ollama model: $model"
      ollama pull "$model"
    fi
  done
}

start_postgres() {
  docker compose up -d postgres
}

wait_for_postgres() {
  until docker compose exec postgres pg_isready -U fruitcake -d fruitcake_v5 >/dev/null 2>&1; do
    sleep 1
  done
}

run_migrations() {
  alembic upgrade head
}

seed_default_users() {
  python "$ROOT/scripts/seed.py"
}

health_url() {
  echo "http://localhost:${APP_PORT}/health"
}

print_header() {
  echo "▶ $1"
}
