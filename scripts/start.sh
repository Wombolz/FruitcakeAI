#!/usr/bin/env bash
# FruitcakeAI v5 — legacy start wrapper
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "▶ scripts/start.sh now delegates to the canonical alpha bootstrap flow."
exec "$ROOT/scripts/bootstrap.sh"
