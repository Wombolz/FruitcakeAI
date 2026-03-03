# FruitcakeAI v5

A private, local-first AI assistant for families. Runs entirely on your hardware — no data leaves your home.

```
iPhone / Mac app  →  FastAPI backend  →  Ollama (local LLM)
                              ↓
                    LlamaIndex RAG (pgvector)
                    MCP tool servers (calendar, web, RSS)
```

---

## Quick Start (under 30 minutes)

### Prerequisites

- macOS with [Homebrew](https://brew.sh)
- [Docker Desktop](https://www.docker.com/products/docker-desktop/)
- [Ollama](https://ollama.ai) — `brew install ollama`
- [Xcode 26+](https://developer.apple.com/xcode/) — for the iOS/macOS app
- Python 3.9+

### 1. Clone and configure

```bash
git clone <repo-url> fruitcake_v5
cd fruitcake_v5
cp .env.example .env
```

Edit `.env` — the only required change for a local setup:

```env
SECRET_KEY=change-me-to-a-random-string
DATABASE_URL=postgresql+asyncpg://fruitcake:fruitcake@localhost:5432/fruitcake_v5
LLM_MODEL=ollama_chat/qwen2.5:14b
LOCAL_API_BASE=http://localhost:11434/v1
EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
```

### 2. Pull the LLM

```bash
ollama pull qwen2.5:14b
ollama serve   # runs in background on port 11434
```

> **M1 Max 64GB note**: `qwen2.5:14b` is the verified default. `qwen2.5:32b` (~20GB) is a step up if you close other apps. `llama3.3:70b` (~43GB) crashes at runtime — avoid it.

### 3. Start the backend

```bash
./scripts/start.sh
```

This script:
1. Starts the Docker postgres container (`pgvector/pgvector:pg16`)
2. Waits for Ollama health check
3. Creates the Python virtual environment and installs dependencies
4. Runs database migrations and seeds default users
5. Starts the FastAPI server on `http://localhost:8000`

Default users:

| Username | Password | Role |
|----------|----------|------|
| admin | changeme123 | admin |
| parent | changeme123 | parent |
| kid | changeme123 | child |
| guest | changeme123 | guest |

> **Change these passwords before using on a shared network.**

### 4. Verify the backend

```bash
curl http://localhost:8000/health
# → {"status": "ok"}

curl -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"changeme123"}'
# → {"access_token": "eyJ...", "token_type": "bearer"}
```

### 5. Open the Swift app

1. Open `../FruitcakeAi/FruitcakeAi.xcodeproj` in Xcode
2. Select your target device (iPhone simulator or Mac)
3. Build and run (`⌘R`)
4. In the app: **Settings → Server URL** → enter `http://localhost:8000`
5. Log in with any seed user

**First chat**: type anything — the assistant responds from the local LLM. Upload a PDF via the Library tab, then ask about it.

---

## Running Tests

```bash
source .venv/bin/activate
pytest tests/
# 48 passed in ~6s — no running PostgreSQL required
```

---

## Architecture

```
Swift App (iOS/macOS)
  ├── AuthManager       JWT auth, Keychain storage
  ├── APIClient         REST calls, multipart upload
  ├── WebSocketManager  Streaming chat tokens
  ├── OnDeviceAgent     FoundationModels fallback (offline mode)
  └── SwiftData         Local message cache

FastAPI Backend (fruitcake_v5/)
  ├── app/api/          HTTP + WebSocket endpoints
  ├── app/agent/        LiteLLM agent loop, tool dispatch, persona system
  ├── app/rag/          LlamaIndex + pgvector hybrid retrieval
  ├── app/mcp/          MCP tool registry (calendar, web, RSS, Docker servers)
  └── app/db/           PostgreSQL models, async sessions
```

**Agent-first**: The LLM orchestrates tools — no hand-written routing rules.
**Local by default**: Ollama + HuggingFace embeddings. No API keys required.
**On-device fallback**: Offline mode uses Apple FoundationModels for calendar/reminders/contacts.

---

## Configuration

| Topic | Guide |
|-------|-------|
| Switch LLM (Ollama → Claude → OpenAI) | [docs/LLM_BACKENDS.md](docs/LLM_BACKENDS.md) |
| Add MCP tools (config-only, no code) | [docs/ADDING_MCP_TOOLS.md](docs/ADDING_MCP_TOOLS.md) |
| Customize personas per user | [docs/PERSONA_SYSTEM.md](docs/PERSONA_SYSTEM.md) |
| Full roadmap | [docs/FruitcakeAI_v5_Roadmap_2.md](docs/FruitcakeAI_v5_Roadmap_2.md) |

---

## GitHub / Repository Setup

- **Secrets**: Real API keys and private configuration should only live in `.env` (and any `*.env.*` files). These files are **git-ignored** and are not committed.
- **Data**: Uploaded documents and derived chunks are stored under `storage/`, which is also **git-ignored** so your personal data never ends up in the repository.
- **Local-only files**: Virtual environments (`.venv/`), caches (`.pytest_cache/`), logs (`logs/`, `*.log`), IDE settings (`.vscode/`, `.idea/`), and local databases/backups (`*.sqlite*`, `*.db`, `*.bak`, `*.backup`) are all ignored by `.gitignore`.

To publish this project to GitHub from a clean clone:

```bash
git init
git add .
git status   # verify .env, storage/, .venv/, logs/, etc. are NOT staged
git commit -m "Initialize FruitcakeAI v5"

# After creating a repo on GitHub:
git remote add origin git@github.com:<your-username>/fruitcake_v5.git
git push -u origin main
```

---

## Reset to clean state

```bash
./scripts/reset.sh   # drops DB, recreates tables, reseeds users
```

---

## Project Structure

```
fruitcake_v5/
├── app/
│   ├── agent/          Agent loop, tools, personas, context builder
│   ├── api/            REST + WebSocket endpoints
│   ├── auth/           JWT auth, user registration
│   ├── db/             SQLAlchemy models, async session
│   ├── mcp/            MCP registry + internal servers
│   └── rag/            LlamaIndex RAG service + hybrid retriever
├── config/
│   ├── mcp_config.yaml     MCP server definitions
│   ├── personas.yaml       Persona definitions
│   └── users.yaml          Seed users
├── tests/              48 tests, SQLite in-memory (no DB required)
├── scripts/
│   ├── start.sh        One-command startup
│   └── reset.sh        Wipe and reseed
└── docker-compose.yml  PostgreSQL + pgvector
```
