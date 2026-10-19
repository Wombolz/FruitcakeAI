#!/usr/bin/env bash
# Maintenance only: resolve reviewed inputs without modifying the app environment.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UV_BIN="${UV_BIN:-uv}"
cd "$ROOT"

if ! command -v "$UV_BIN" >/dev/null 2>&1; then
  echo "Install uv 0.13.1 in a maintenance environment, or set UV_BIN to its path." >&2
  exit 1
fi
if [[ "$("$UV_BIN" --version | awk '{print $2}')" != "0.13.1" ]]; then
  echo "Use uv 0.13.1 for reproducible dependency generation." >&2
  exit 1
fi

stamp_inputs() {
  local output="$1" temporary
  shift
  temporary="$(mktemp "${output}.XXXXXX")"
  {
    for input in "$@"; do
      printf '# Input SHA256: %s\n' "$(shasum -a 256 "$input")"
    done
    cat "$output"
  } > "$temporary"
  mv "$temporary" "$output"
}

"$UV_BIN" pip compile requirements.in --upgrade --python-version 3.11 \
  --universal --generate-hashes --no-build \
  --custom-compile-command scripts/update_dependencies.sh -o requirements.txt
stamp_inputs requirements.txt requirements.in
"$UV_BIN" pip compile requirements-dev.in --upgrade --python-version 3.11 \
  --universal --generate-hashes --no-build \
  --custom-compile-command scripts/update_dependencies.sh -o requirements-dev.txt
stamp_inputs requirements-dev.txt requirements.in requirements-dev.in requirements.txt

echo "Generated runtime and development baselines. Audit and test in a fresh environment before rollout."
