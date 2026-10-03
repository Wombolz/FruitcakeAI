# LLM Backends

Switch the underlying model with `.env` settings. No code changes required.

---

## Currently verified backends

### Ollama — local, private, no API key (default)

```env
LLM_MODEL=ollama_chat/qwen2.5:14b
LOCAL_API_BASE=http://localhost:11434/v1
LOCAL_MODEL_KEEP_ALIVE=15m
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

### Ollama prompt-cache behavior

Fruitcake keeps the leading local-model system prompt stable and moves
turn-specific time, grounding, skill, and guardrail context to the latest user
turn. This gives Ollama a reusable prompt prefix while preserving the full
request context. Cloud-provider message construction is unchanged.

`LOCAL_MODEL_KEEP_ALIVE` controls how long Ollama should keep a local chat model
resident after a request. The default is `15m`; set it to an empty value to use
Ollama's own default. Incognito sessions do not override Ollama's keep-alive
policy.

When Ollama returns native timing data, the backend logs:

- `agent.prompt_cache_shape`: stable-prefix and tool-schema fingerprints only
- `llm.local_inference_timing`: prompt tokens, cached prompt tokens, cache hit
  percentage, model-load time, prompt-evaluation time, and generation time

These diagnostics do not log prompt text. This optimization uses Ollama's
in-memory model and prompt cache only; Fruitcake does not persist a prompt cache
to disk.

For a direct local benchmark:

```bash
.venv/bin/python scripts/benchmark_ollama_prompt_cache.py \
  --model qwen3.6:35b
```

### Experimental native agent streaming

Fruitcake can opt selected models into one provider stream per agent turn. This
removes the compatibility path's non-streaming probe and duplicate final-text
request while preserving complete tool-call accumulation before dispatch.

The feature is disabled by default and model allowlisted:

```env
FRUITCAKE_NATIVE_AGENT_STREAMING_ENABLED=true
FRUITCAKE_NATIVE_AGENT_STREAMING_MODELS=ollama_chat/muse-glimmer:30b-mlx
FRUITCAKE_NATIVE_AGENT_STREAMING_REASONING_EFFORT=high
```

Optional developer-only reasoning output can be sent to stderr:

```env
FRUITCAKE_LOCAL_REASONING_TAP=true
```

The reasoning tap is never persisted by the streaming transport and is
automatically disabled for incognito sessions. It is diagnostic model output,
not an audit log or a literal representation of model inference.
Reasoning is buffered until the provider stream ends (including interruption),
then redacted and written to stderr so credentials split across deltas are
redacted together.

Before enabling a new model in normal chat, run its streamed fixture matrix:

```bash
.venv/bin/python scripts/diagnose_local_tool_calls.py \
  --model muse-glimmer:30b-mlx \
  --legs litellm_stream \
  --trials 1
```

Models not present in `FRUITCAKE_NATIVE_AGENT_STREAMING_MODELS` continue using
the established compatibility path. A native stream may fall back only if it
fails before producing its first event; partial turns are never replayed.

For tool-enabled native turns, the WebSocket uses reversible `draft_token`,
`draft_reset`, and `draft_commit` events. This lets clients display model text
immediately while still removing intermediate narration if the completed turn
selects a tool. The terminal `done` event remains authoritative and contains the
complete persisted answer for backward compatibility.

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

OpenAI prompt caching is automatic for matching request prefixes. Fruitcake
keeps the persona and policy system prefix stable, places volatile turn context
in a following system message, and sends a hashed `prompt_cache_key` derived
from the stable prompt and tool-schema shape. Prompt content is not included in
the key or cache diagnostics.

```env
OPENAI_PROMPT_CACHE_ENABLED=true
# Optional; empty preserves the provider/organization default.
OPENAI_PROMPT_CACHE_RETENTION=
```

Set an explicit retention value only after checking that it is supported by the
selected model and appropriate for the deployment's privacy policy. Fruitcake
does not send cache-routing or retention hints for incognito sessions, but
OpenAI's automatic caching and organization-level retention policy still apply.
Cached input usage is logged through `llm.prompt_cache_usage` when OpenAI
returns `prompt_tokens_details.cached_tokens`.

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
