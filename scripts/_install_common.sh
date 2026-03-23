#!/usr/bin/env bash
# Shared install/bootstrap helpers for FruitcakeAI pre-alpha onboarding.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_PORT="${APP_PORT:-30417}"
OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-http://localhost:11434}"
ENV_FILE="$ROOT/.env"
ENV_TEMPLATE="$ROOT/.env.example"

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

ensure_venv() {
  if [[ ! -d "$ROOT/.venv" ]]; then
    python3.11 -m venv "$ROOT/.venv"
  fi
  # shellcheck disable=SC1091
  source "$ROOT/.venv/bin/activate"
  if [[ ! -f "$ROOT/.venv/.fruitcake-deps-installed" ]]; then
    pip install -q -r "$ROOT/requirements.txt"
    touch "$ROOT/.venv/.fruitcake-deps-installed"
  fi
}

ensure_models() {
  local models
  mapfile -t models < <(required_model_list "$(ram_tier)")
  if [[ -f "$ENV_FILE" ]]; then
    if grep -q '^LLM_MODEL=ollama_chat/' "$ENV_FILE"; then
      models=()
      local llm_model task_small task_large
      llm_model="$(sed -n 's/^LLM_MODEL=ollama_chat\///p' "$ENV_FILE" | head -n 1)"
      task_small="$(sed -n 's/^TASK_SMALL_MODEL=ollama_chat\///p' "$ENV_FILE" | head -n 1)"
      task_large="$(sed -n 's/^TASK_LARGE_MODEL=ollama_chat\///p' "$ENV_FILE" | head -n 1)"
      [[ -n "$llm_model" ]] && models+=("$llm_model")
      [[ -n "$task_small" ]] && models+=("$task_small")
      [[ -n "$task_large" ]] && models+=("$task_large")
    fi
  fi

  local unique_models=()
  local seen=""
  local model
  for model in "${models[@]}"; do
    [[ -z "$model" ]] && continue
    if [[ " $seen " != *" $model "* ]]; then
      seen+=" $model"
      unique_models+=("$model")
    fi
  done

  local tags
  tags="$(curl -sf "$OLLAMA_BASE_URL/api/tags" || true)"
  for model in "${unique_models[@]}"; do
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
