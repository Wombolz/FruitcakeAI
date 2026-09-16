# Release Notes v0.7.32

## Summary

This release adds experimental native model streaming for simple chat turns, with complete tool-call accumulation, reversible client drafts, and Ollama reasoning isolation.

## Included Changes

- added a disabled-by-default native-streaming feature flag and exact model allowlist
- used one provider request per native agent turn, avoiding the compatibility path's separate final-text request where applicable
- added `draft_token`, `draft_reset`, and `draft_commit` WebSocket events so clients can display text before tool selection is known
- retained the complete persisted answer in `done` and added a `tool_completed` live state
- patched LiteLLM's Ollama stream translation to preserve reasoning boundaries and keep complete calls distinct across chunks
- buffered optional local reasoning diagnostics until stream completion or interruption so credential redaction works across delta boundaries
- closed provider streams on cancellation and prevented compatibility replay after partial progress
- refreshed routing preferences between messages on an existing WebSocket connection
- added sanitized stream-shape diagnostics and regression tests using the installed LiteLLM parser

## Notes

- native streaming remains opt-in through `FRUITCAKE_NATIVE_AGENT_STREAMING_ENABLED` and `FRUITCAKE_NATIVE_AGENT_STREAMING_MODELS`; validation-gated and orchestrated execution retain their existing paths
- clients must implement the draft events to display provisional text; `done.content` remains authoritative for older clients
- the local reasoning tap is disabled by default, suppressed for incognito sessions, and writes redacted diagnostics only when the provider stream ends

## Verification

- `.venv/bin/python -m pytest -q tests/test_model_stream.py tests/test_chat_streaming.py tests/test_auth.py tests/test_agent.py --disable-warnings`: 215 passed
- Python compilation passed for `app/`, `tests/`, and the local tool-call diagnosis script
- `git diff --check` passed
- provider regressions use controlled fixtures through the installed LiteLLM parser; no live-model qualification was performed for this release
