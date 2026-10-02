# Release Notes v0.7.34

## Summary

This release improves local Ollama responsiveness by making Fruitcake's prompt layout cache-friendly, keeping models resident between active turns, and exposing native cache and inference timing diagnostics.

## Included Changes

- separated stable local system policy from volatile turn-specific context
- moved current time, grounding, skill instructions, and local guardrails into a trusted context block on the latest user turn
- preserved the existing cloud-provider prompt layout
- retained compatibility with strict local chat templates that require one leading system message
- added `LOCAL_MODEL_KEEP_ALIVE`, defaulting to `15m` for normal local-model requests
- omitted Fruitcake's keep-alive override for incognito sessions
- preserved Ollama's cached prompt-token and timing fields through LiteLLM response translation
- added sanitized prompt-prefix and tool-schema fingerprints plus cache-hit and inference-stage timing logs
- added a standalone local prompt-cache benchmark and focused regression coverage

## Notes

- the optimization uses Ollama's in-memory model and prompt cache; Fruitcake does not create a persistent prompt cache on disk
- cache diagnostics log fingerprints and counts, not prompt or tool-schema content
- cloud-model request construction remains unchanged
- operators can set `LOCAL_MODEL_KEEP_ALIVE=` to defer to Ollama's default behavior

## Verification

- 264 relevant agent, routing, streaming, usage, and chat tests passed
- the live `qwen2.5:14b` benchmark reused 43 of 50 prompt tokens on the repeated stable-prefix request, an 86% cache hit rate
- the same benchmark completed the warm repeated-prefix request in approximately 361 ms versus approximately 5.35 seconds for the cold request
- Python compilation and `git diff --check` passed
- three unrelated stale tests remain: two loop-message assertions and one chat mock that does not accept the existing `pre_tool_callback` argument
