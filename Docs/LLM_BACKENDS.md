# LLM Backends

Switch the underlying model by changing two lines in `.env`. No code changes required.

---

## Currently verified backends

### Ollama — local, private, no API key (default)

```env
LLM_MODEL=ollama_chat/qwen2.5:14b
LOCAL_API_BASE=http://localhost:11434/v1
```

```bash
ollama pull qwen2.5:14b
ollama serve
```

**Hardware requirements (M1 Max 64GB)**:

| Model | VRAM | Status |
|-------|------|--------|
| `qwen2.5:14b` | ~9GB | ✅ Verified default |
| `qwen2.5:32b` | ~20GB | ✅ Works (close other apps) |
| `qwen2.5:72b` | ~44GB | ⚠️ May crash — test first |
| `llama3.3:70b` | ~43GB | ❌ Crashes with pgvector + embedding in RAM |

> **Important**: Use the `ollama_chat/` prefix, not `ollama/`. The `ollama/` prefix routes to the generate API which does not support tool/function calling — tools will be silently ignored.

---

### Anthropic Claude — cloud, best quality

```env
LLM_MODEL=claude-sonnet-4-6
ANTHROPIC_API_KEY=sk-ant-...
# Leave LOCAL_API_BASE unset or blank
```

No `ollama serve` needed. Requires internet access and an Anthropic API key.

```bash
# Unset the local base so LiteLLM routes to Anthropic's API
LOCAL_API_BASE=
```

---

### OpenAI — cloud, widely compatible

```env
LLM_MODEL=gpt-4o
OPENAI_API_KEY=sk-...
LOCAL_API_BASE=
```

---

### Any OpenAI-compatible local server

```env
LLM_MODEL=openai/your-model-name
LOCAL_API_BASE=http://localhost:1234/v1   # LM Studio, vLLM, etc.
OPENAI_API_KEY=not-needed                 # some servers require a placeholder
```

---

## How the backend selects the model

`app/agent/core.py` calls `_litellm_kwargs()` on every LLM request:

```python
def _litellm_kwargs(self) -> dict:
    base = settings.local_api_base.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return {"api_base": base, "model": settings.llm_model}
```

- If `LOCAL_API_BASE` is set, it's passed as `api_base` — LiteLLM routes there
- If `LOCAL_API_BASE` is blank/unset, LiteLLM routes based on the model prefix
  (`claude-` → Anthropic, `gpt-` → OpenAI, etc.)

---

## Embeddings

The embedding model is independent of the chat LLM and always runs locally via HuggingFace:

```env
EMBEDDING_MODEL=BAAI/bge-small-en-v1.5   # ~130MB, fast, good quality
# EMBEDDING_MODEL=BAAI/bge-large-en-v1.5  # ~1.3GB, higher quality
```

The embedding model is downloaded to `~/.cache/huggingface/` on first startup.
It runs in a thread executor so it doesn't block the event loop.

**Do not change the embedding model** after documents have been indexed — the
vector dimensions must match. If you change models, run `./scripts/reset.sh`
to reindex.

---

## Verifying your backend is working

```bash
curl http://localhost:30417/admin/health \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

Look for:

```json
{
  "status": "ok",
  "database": "ok",
  "llm": "ok",
  "embedding_model": "ready",
  "mcp": "12 tools"
}
```

If `"llm": "error"`, check:
1. `ollama serve` is running (for Ollama)
2. `LOCAL_API_BASE` matches where Ollama is listening
3. The model has been pulled (`ollama list`)
