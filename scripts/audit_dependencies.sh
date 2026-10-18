#!/usr/bin/env bash
# Audit the active environment, including transitive dependencies and test tools.
set -euo pipefail

# NLTK's unpatched model-path sandbox issue is not used by Fruitcake's tokenizer.
# Do not extend this exception without rechecking upstream and application usage.
python - <<'PY'
from datetime import date
if date.today() > date(2026, 11, 18):
    raise SystemExit("Review the expired NLTK exception in Docs/dependencies.md before auditing.")
PY
exec python -m pip_audit --local --progress-spinner=off \
  --ignore-vuln PYSEC-2026-3740 "$@"
