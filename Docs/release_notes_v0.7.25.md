# Release Notes v0.7.25

## Summary

This release closes the current local-chat stabilization branch by making tool-backed local sessions more durable, easier to recover, and less likely to loop or silently fail when document summaries and RSS-heavy turns push the runtime hard.

## Included Changes

- chat now persists runtime assistant tool-call rows and tool-result rows incrementally during both REST and websocket turns, so successful tool work is retained even if final local synthesis fails later in the turn
- local post-tool synthesis failures now recover with a visible assistant response instead of losing the turn outright, preserving the saved evidence in session history for follow-up
- large `summarize_document` follow-ups now feed local synthesis through a compact evidence digest rather than a full raw tool payload, reducing local overload while keeping the answer grounded in the retrieved document
- `summarize_document` now uses evenly sampled large-document chunks, a more structured evidence-oriented summarization prompt, and a narrower hygiene pass for unsupported totals such as case-study/test counts
- added configurable text-only local model support through `LOCAL_TOOL_TEXT_ONLY_MODELS`, plus a safe fallback path for local models that explicitly report that they do not support tools
- chat validation now catches tool-backed continuation narration such as “Let me try another source...” and retries toward either a real next tool call or a final user-facing answer
- mixed RSS headline-roundup turns now converge correctly instead of looping across repeated `search_my_feeds` / `list_recent_feed_items` style batches, including prompts phrased as a “round up” rather than only “headlines”
- fixed a cloud-safety regression by making `DOCUMENT_SUMMARY_MODEL` optional again, with empty/default behavior falling back to `LLM_MODEL`
- fixed runtime-history flush bookkeeping so incremental persistence tracks consumed runtime messages rather than persisted row count, preventing duplicate tool rows when non-persistable runtime messages appear mid-turn

## Notes

- the branch’s higher default chat/tool/filesystem limits remain the chosen product baseline for now:
  - `agent_tool_result_max_chars = 4000`
  - `chat_history_soft_token_limit = 32000`
  - `filesystem_mcp_max_read_bytes = 250000`
  - `filesystem_mcp_max_write_bytes = 240000`
- deeper root-cause work for Qwen/Ollama/LiteLLM tool-calling incompatibilities remains a follow-up; this release keeps the current safe fallback layer in place while that investigation continues
- focused backend verification for the touched runtime/chat surfaces passed with:
  - `187 passed` across `tests/test_agent.py`, `tests/test_auth.py`, `tests/test_chat_streaming.py`, and `tests/test_chat_validation.py`
