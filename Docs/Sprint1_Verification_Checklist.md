# Sprint 1 Verification Checklist

Use this to verify Phase 1 (Sprint 1.1–1.4) from [FruitcakeAI_v5_Roadmap_1.md](FruitcakeAI_v5_Roadmap_1.md).

---

## Quick verification (automated)

From the project root with a venv activated:

```bash
# Structure-only checks (no server or DB needed)
python3 scripts/verify_sprint1.py --structure-only

# With server running (terminal 1: docker compose up -d postgres && uvicorn app.main:app --port 8000)
python3 scripts/verify_sprint1.py
```

---

## Acceptance criteria (from roadmap)

### Sprint 1.1 — Project Bootstrap ✅ / ⬜

| Check | How to verify |
|-------|----------------|
| `docker-compose up` starts postgres | `docker compose up -d postgres` then `docker compose ps` |
| `uvicorn app.main:app` starts without errors | `uvicorn app.main:app --host 127.0.0.1 --port 8000` |
| `/health` returns 200 | `curl http://127.0.0.1:8000/health` → `{"status":"ok", ...}` |

**Current status:** Bootstrap is in place: `app/main.py`, `app/config.py`, `requirements.txt`, `.env.example`, `docker-compose.yml` (postgres + pgvector), DB models, auth skeleton. Root `/health` is implemented.

---

### Sprint 1.2 — Auth System ✅ / ⬜

| Check | How to verify |
|-------|----------------|
| Login returns token | `POST /auth/login` with valid credentials → JWT in response |
| Protected routes reject unauthenticated requests | `GET /auth/me` without `Authorization: Bearer <token>` → 401 |
| User roles stored in DB | User model has `role`; seed or create user with role, then `/auth/me` returns it |

**Current status:** JWT helpers (`app/auth/jwt.py`) and auth dependencies (`get_current_user`, `require_admin`) are implemented. **Missing:** `POST /auth/login`, `GET /auth/me` (and optional `POST /auth/register`) in `app/auth/router.py`, and a seed script (e.g. from `config/users.yaml`).

---

### Sprint 1.3 — LlamaIndex RAG Service ✅ / ⬜

| Check | How to verify |
|-------|----------------|
| Upload a PDF | `POST /library/ingest` with file → document stored, chunks created |
| Query returns relevant chunks with source citations | `GET /library/query?q=...` → results with citations |
| User A cannot see user B's documents | Two users; upload as A, query as B → no results for B's docs (or 403) |

**Current status:** `config/rag_config.yaml` exists. **Missing:** Implementations in `app/rag/service.py`, `app/rag/retriever.py`, `app/rag/ingest.py`, and Library API in `app/api/library.py` (`POST /library/ingest`, `GET /library/query`, `GET /library/documents`, `DELETE /library/documents/{id}`). Mount library router in `app/main.py`.

---

### Sprint 1.4 — Agent Core ✅ / ⬜

| Check | How to verify |
|-------|----------------|
| Send a chat message | `POST /chat/sessions` then `POST /chat/messages` (or WebSocket) |
| Agent calls `search_library` when relevant | Ask a question about your documents; response or logs show tool call |
| Response includes citations | Answer references library chunks |
| Conversation history maintained per session | Multiple messages in same session; context preserved |

**Current status:** `app/agent/context.py` has `UserContext` and `to_system_prompt()`. **Missing:** `app/agent/core.py` (agent loop with LiteLLM + tool dispatch), `app/agent/tools.py` (tool registry, `search_library`), Chat API in `app/api/chat.py` (`POST /chat/sessions`, `POST /chat/messages`, WebSocket streaming). Mount chat router in `app/main.py`.

---

## Manual run-through

1. **Start stack**
   - `./scripts/start.sh` (or `docker compose up -d postgres` then `uvicorn app.main:app --port 8000`).
   - Note: `start.sh` runs `alembic upgrade head`; if no migration versions exist, create an initial migration or rely on `create_all` in lifespan.

2. **Health**
   - `curl http://127.0.0.1:8000/health`
   - `curl http://127.0.0.1:8000/admin/health`

3. **Auth** (once login/me are implemented)
   - `curl -X POST http://127.0.0.1:8000/auth/login -H "Content-Type: application/json" -d '{"username":"...","password":"..."}'`
   - `curl http://127.0.0.1:8000/auth/me -H "Authorization: Bearer <token>"`

4. **Library** (once RAG + library API are implemented)
   - Upload: `curl -X POST http://127.0.0.1:8000/library/ingest -H "Authorization: Bearer <token>" -F "file=@sample.pdf"`
   - Query: `curl "http://127.0.0.1:8000/library/query?q=your+query" -H "Authorization: Bearer <token>"`

5. **Chat** (once agent + chat API are implemented)
   - Create session, post message, confirm tool calls and citations.

6. **Tests**
   - `pytest tests/ -v` (run with venv and deps installed).

---

## Summary: what’s done vs remaining

| Sprint | Done | Remaining |
|--------|------|-----------|
| 1.1 | App skeleton, config, deps, docker-compose, DB models, `/health` | Optional: first Alembic migration so `alembic upgrade head` works |
| 1.2 | JWT, auth dependencies, User model with roles | Auth router: `POST /auth/login`, `GET /auth/me`; seed script |
| 1.3 | `config/rag_config.yaml` | RAG service, retriever, ingest; Library API; mount in main |
| 1.4 | `UserContext` and system prompt in `context.py` | Agent core loop, tools (e.g. `search_library`), Chat API + WebSocket; mount in main |

Run `python3 scripts/verify_sprint1.py --structure-only` anytime to re-check structure against this checklist.
