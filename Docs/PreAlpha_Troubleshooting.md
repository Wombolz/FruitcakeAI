# Pre-Alpha Troubleshooting

Use this guide when `./scripts/bootstrap.sh` or `./scripts/doctor.sh` does not produce a healthy backend.

---

## Docker is missing or not running

**Symptom**
- bootstrap says `docker` is missing
- or doctor reports Docker daemon is not reachable

**Likely cause**
- Docker Desktop is not installed
- or Docker Desktop is installed but not started

**Fix**
```bash
open -a Docker
```

Then rerun:
```bash
./scripts/bootstrap.sh
```

---

## Ollama is missing or not reachable

**Symptom**
- bootstrap says `ollama` is missing
- or doctor reports Ollama is not reachable

**Likely cause**
- Ollama is not installed
- or `ollama serve` is not running

**Fix**
```bash
brew install ollama
ollama serve
```

Then rerun:
```bash
./scripts/bootstrap.sh
```

---

## Required local model is missing

**Symptom**
- doctor reports a missing required Ollama model

**Likely cause**
- the model was not pulled yet
- or a previous pull failed

**Fix**
```bash
ollama pull qwen2.5:14b
ollama pull qwen2.5:32b
```

If your machine has less than 64 GB RAM, only `14b` is required by default.

---

## Migrations are not at head

**Symptom**
- doctor reports Alembic is not at head
- or startup fails during migration

**Likely cause**
- backend was upgraded without running migrations
- or the local database state is stale

**Fix**
```bash
source .venv/bin/activate
alembic upgrade head
```

If the database state is intentionally disposable in local development:
```bash
./scripts/reset.sh
```

---

## `.env` is missing or wrong

**Symptom**
- doctor reports `.env` missing
- or the backend boots with the wrong model/backend settings

**Likely cause**
- first-run bootstrap has not been completed
- or `.env` was edited incorrectly

**Fix**
```bash
cp .env.example .env
./scripts/bootstrap.sh
```

If `.env` already exists, bootstrap will not overwrite it. Edit the values manually if needed.

---

## Shell MCP image is missing

**Symptom**
- doctor reports shell MCP image missing

**Likely cause**
- the optional local shell image has not been built yet

**Fix**
```bash
docker build -t fruitcake/mcp-shell -f mcp_shell_server/Dockerfile .
```

This is optional. The backend can still run without shell MCP support.

---

## Backend `/health` is not healthy

**Symptom**
- `curl http://localhost:30417/health` fails
- or doctor reports backend health failure

**Likely cause**
- backend process is not running
- or startup failed before Uvicorn finished booting

**Fix**
Start or restart the backend:
```bash
./scripts/bootstrap.sh
```

If the process exits early, inspect the terminal output for the first failing dependency.

---

## HTTPS helper cannot start because `caddy` is missing

**Symptom**
- `./scripts/start_https.sh` says `caddy` is missing

**Likely cause**
- Caddy is not installed yet

**Fix**
```bash
brew install caddy
./scripts/start_https.sh
```

---

## HTTPS URL loads with an untrusted certificate warning

**Symptom**
- browser or client warns that the Fruitcake certificate is not trusted
- this usually happens on the default local/LAN path using `tls internal`

**Likely cause**
- Caddy's local certificate authority has not been trusted on that machine yet

**Fix**
```bash
sudo caddy trust
```

Then retry the HTTPS URL.

If you are using a public domain, make sure you removed `tls internal` from `Caddyfile` so Caddy can obtain a normal public certificate.
