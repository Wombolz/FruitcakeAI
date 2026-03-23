# Pre-Alpha Installability Brief

**Status**: Planned  
**Phase**: Pre-alpha stabilization  
**Depends on**: current backend startup flow, README quick start, admin health endpoints

---

## Summary

Turn the current backend startup path into a true pre-alpha installation flow for early adopters.

Primary alpha target:
- macOS Apple Silicon
- backend-first local install
- Swift client setup only after backend success

This brief does not add product features. It locks the installation, diagnostics, and documentation behavior needed for a stable alpha handoff.

Canonical operator flow:
1. install prerequisites
2. run one bootstrap command
3. let bootstrap create or verify the local runtime state
4. verify backend health
5. optionally connect the Swift client

Linux note:
- Linux backend install appears architecturally plausible, but is not a supported pre-alpha target yet
- current Linux gaps are documented here so the implementation does not accidentally overclaim support

---

## Goals

1. Reduce clone-to-running friction for alpha users to one canonical backend bootstrap command.
2. Make first-run failures actionable instead of requiring manual debugging.
3. Choose local model defaults automatically based on available RAM so lower-memory machines do not fail by default.
4. Distinguish required vs optional components so missing extras do not block backend startup.
5. Rewrite the quick start docs around the actual supported alpha path.

---

## Locked Decisions

- The primary supported alpha platform is **macOS Apple Silicon**.
- The primary alpha success target is **backend-first installability**, not full-stack client onboarding.
- The Swift client is documented as a second-step optional companion after backend success.
- The current `scripts/start.sh` is not the final alpha entrypoint.
- There must be one canonical alpha startup command documented in `README.md`.
- Bootstrap must be **non-destructive** to an existing `.env`; first-run defaults are only written when `.env` does not yet exist.
- RAM-aware model defaults are selected automatically during first bootstrap; there is no required interactive RAM prompt.
- Missing optional components must degrade clearly, not fail the entire install.
- Linux is not a supported alpha target and must not expand the scope of this branch.

---

## Current State

The backend already has a useful but developer-oriented startup path:
- `scripts/start.sh` starts Postgres, tries to start Ollama, creates `.venv`, installs dependencies, runs migrations, seeds users, and launches Uvicorn
- `README.md` still assumes manual `.env` editing and manual model pulls
- `/health` and `/admin/health` already exist and can support verification

The current problems are:
- no true preflight or operator-facing diagnostics path
- no required vs optional dependency classification
- default local model settings assume enough RAM for `32b`
- install docs are still closer to a developer runbook than an alpha onboarding path

---

## Scope

### In scope
- one canonical backend bootstrap command
- one diagnostics/preflight command
- RAM-aware first-run local model defaults
- model pull policy tied to detected RAM tier
- required vs optional component classification
- README quick start rewrite around the supported alpha path
- short troubleshooting doc for common operator failures
- Linux readiness note and blocker list

### Not in scope
- new product capabilities
- Linux support implementation
- Windows support
- Swift client install automation
- cloud LLM setup as part of the primary alpha flow
- changing backend APIs for this branch

---

## Contract

### Alpha install target
- supported pre-alpha install target:
  - macOS Apple Silicon
- supported pre-alpha success target:
  - backend reaches healthy local startup and is usable via API/login
- secondary/non-blocking path:
  - connect the Swift client after backend success
- unsupported-for-alpha-but-plausible:
  - Linux backend install

### Bootstrap contract
The canonical bootstrap flow must:
- check for:
  - `docker`
  - `ollama`
  - `python3.11`
- verify Docker daemon status
- verify Ollama reachability
- start `ollama serve` if needed and if a local install supports it
- create `.venv` if missing
- install Python dependencies if missing
- create `.env` from `.env.example` if missing
- preserve an existing `.env` without overwriting model or secret values
- start Postgres with `docker compose`
- run `alembic upgrade head`
- run `scripts/seed.py`
- start the backend API

Bootstrap behavior rules:
- bootstrap is the canonical alpha entrypoint
- `scripts/start.sh` must either become a thin wrapper around bootstrap or remain only as a developer convenience path
- the README must document only one canonical alpha startup command
- rerunning bootstrap must be safe and idempotent for an already-initialized local setup

### RAM-aware model defaults
RAM is detected automatically on first bootstrap.

If detected RAM is **less than 64 GB**:
- `LLM_MODEL=ollama_chat/qwen2.5:14b`
- `TASK_SMALL_MODEL=ollama_chat/qwen2.5:14b`
- `TASK_LARGE_MODEL=ollama_chat/qwen2.5:14b`
- required pull set:
  - `qwen2.5:14b`

If detected RAM is **64 GB or greater**:
- `LLM_MODEL=ollama_chat/qwen2.5:32b`
- `TASK_SMALL_MODEL=ollama_chat/qwen2.5:14b`
- `TASK_LARGE_MODEL=ollama_chat/qwen2.5:32b`
- required pull set:
  - `qwen2.5:14b`
  - `qwen2.5:32b`

RAM-tier behavior rules:
- RAM-derived defaults are written only when `.env` does not yet exist
- an existing `.env` is preserved as the source of truth on rerun
- bootstrap must print the detected RAM tier and the chosen model set
- bootstrap should tell the operator how to override the defaults manually

### Diagnostics / preflight contract
A separate diagnostics command must exist and must not require starting the full bootstrap flow.

Diagnostics must check:
- Python version
- `.venv`
- `.env`
- Docker daemon
- Postgres container state
- Ollama reachability
- required models for the configured RAM tier or current `.env`
- Alembic head state
- `/health`
- `/admin/health` when practical
- MCP readiness split into required vs optional

Diagnostics output must include:
- overall status:
  - pass
  - degraded
  - fail
- exact next-step action for each failed check

Dependency classification:
- required:
  - Python 3.11
  - Docker / compose
  - Postgres container
  - Ollama
  - required local model set for the chosen tier
- optional:
  - shell MCP image
  - APNs config
  - Swift client
  - cloud LLM credentials
- degraded-but-usable:
  - optional shell MCP missing
  - APNs not configured
  - Swift client not installed

### Optional component rules
- missing shell MCP image must not fail backend install
- APNs configuration must not be treated as a backend install blocker
- APNs is outbound push delivery and remains compatible with a Linux backend in principle
- Apple device/client requirements do not make the backend macOS-only

### Linux readiness note
The brief must document Linux readiness as follows:
- backend architecture is portable in principle
- Linux is not part of the supported pre-alpha install target
- current Linux blockers/gaps are:
  - Homebrew-oriented README assumptions
  - `python3.11` install/path assumptions
  - `ollama serve` process-management assumptions
  - no tested Linux bootstrap flow
  - no Linux package-manager-specific instructions
- this note is informational and must not expand this branch into Linux support work

---

## Guardrails

- Do not add new product features in this branch.
- Do not silently overwrite an existing `.env`.
- Do not default low-memory machines to `32b`.
- Do not require optional services or images for a healthy backend alpha install.
- Do not claim Linux support in docs or scripts during this branch.
- Keep one canonical alpha install path; avoid competing startup instructions.

---

## Acceptance Criteria

1. A macOS Apple Silicon alpha user can go from clone to healthy backend with one canonical bootstrap command.
2. Systems with less than 64 GB RAM do not default to `32b` local models.
3. Bootstrap is safe to rerun and does not clobber an existing `.env`.
4. Missing optional shell MCP support does not block backend startup.
5. A separate diagnostics/preflight command exists and gives actionable failure output.
6. `README.md` matches the actual bootstrap behavior exactly.
7. Linux is documented as plausible but unsupported, with concrete blocker notes.

---

## Test Plan

### Bootstrap scenarios
- clean setup:
  - no `.venv`
  - no `.env`
  - Postgres not running
  - required models missing
- rerun/idempotent setup:
  - `.venv` exists
  - `.env` exists
  - models already present
- RAM-tier scenarios:
  - simulated `< 64 GB`
  - simulated `>= 64 GB`

### Failure handling scenarios
- Docker missing
- Docker daemon stopped
- Ollama missing
- Ollama unreachable
- model pull failure
- Alembic failure
- optional shell image missing

### Acceptance verification
- `/health` reports ok after bootstrap
- admin health reflects backend readiness clearly enough for operator verification
- README quick start produces the documented result without extra hidden steps
- existing `.env` values remain intact across reruns

---

## Follow-on Work

After this branch proves out, likely follow-ons are:
- supported Linux backend install path
- client-first onboarding improvements
- packaged install flow or release artifacts
- richer diagnostics for optional MCP dependencies

Do not treat this brief as permission to expand into general platform support before the alpha install path is stable.
