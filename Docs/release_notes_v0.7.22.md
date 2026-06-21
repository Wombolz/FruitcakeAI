# Release Notes v0.7.22

## Summary

This release stabilizes local qwen-backed chat turns by preventing a known class of Ollama tool-calling failures from crashing the session, while preserving a clear follow-up path for the deeper root-cause investigation.

## Included Changes

- local `ollama` / `ollama_chat` final text streaming now skips the second final-stream pass and streams the already-probed completion directly
- known Ollama tool-call JSON parse failures now retry as text-only, context-grounded answers instead of aborting the chat turn
- risky `ollama_chat/qwen3.6:35b` workspace and library follow-up prompts are preemptively downgraded to text-only to avoid the current malformed tool-call path
- structured local-tool diagnostics now record prompt class, failure phase, offered tools, and sanitized previews to support a later root-cause pass

## Notes

- focused chat streaming tests passed before release
- this is a temporary stabilization release, not the final fix for local-model tool-calling reliability
- the underlying Ollama/LiteLLM tool-calling failure should remain tracked as follow-up work
