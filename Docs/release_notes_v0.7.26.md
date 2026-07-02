# Release Notes v0.7.26

## Summary

This release closes the current Qwen/Ollama root-cause investigation by patching a lossy LiteLLM request translation bug, making strict-template local models renderable again, and restoring full hybrid RAG fusion instead of silently falling back to vector-only retrieval.

## Included Changes

- patched LiteLLM's `ollama_chat` request transformation so prior assistant `tool_calls` and tool-result `tool_name` survive into Ollama-bound history, fixing corrupted multi-turn local tool transcripts
- coalesced Ollama-bound system messages into a single leading system message so strict Jinja-template local models no longer 400 on valid Fruitcake grounding and guardrail notes
- added a rerunnable Qwen/Ollama diagnosis harness and focused regression tests covering tool-history preservation, tool-result identity restoration, and system-message coalescing
- kept the existing reactive local fallback behavior in place while narrowing the root cause from "Qwen is unstable" to specific LiteLLM/Ollama integration faults and strict-template model behavior
- fixed RAG hybrid search configuration so `rrf` resolves through the installed llama-index `FUSION_MODES` enum before retriever construction instead of failing on first use and permanently degrading to vector-only search
- updated the canonical RAG fusion configuration value to `reciprocal_rerank` while keeping `rrf` as a compatible alias

## Notes

- focused verification passed before release:
  - `tests/test_agent.py -k 'litellm_ollama or local_tool'`
  - `tests/test_chat_streaming.py -k 'local_tool'`
  - `tests/test_rag.py tests/test_library_api.py`
- total focused result count for this release slice: `33 passed`
- the LiteLLM Ollama patch is intentionally narrow and designed to degrade to a no-op if upstream behavior is fixed later
- deeper follow-up work remains for unsupported-tool model behavior and any future upstream cleanup once the patched local path has soaked in real use
