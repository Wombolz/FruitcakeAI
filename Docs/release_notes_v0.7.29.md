# Release Notes v0.7.29

## Summary

This release ships three backend foundation slices together: admin incognito chat sessions, the first Memory v2 write/retrieval overhaul, and the backend metadata primitives for the upcoming richer chat interface. It also includes a small but important developer-experience fix so normal workspace writes no longer trigger reload churn during active work.

## Included Changes

- added admin-only incognito chat sessions with blocked persistent side effects and ephemeral transcript cleanup on delete
- replaced unbounded tiered memory injection with Memory v2 foundations: one enforced write path, dedup and supersede handling, directive caps, ranked budgeted retrieval, and recalled-memory assistant metadata
- added a rerunnable memory evaluation harness with baseline quality numbers and fixes for two defects it exposed in dedup and lexical relevance
- added additive websocket live-state events and structured assistant evidence metadata to support the next richer chat UI phase
- excluded `workspace/*` and `storage/*` from the dev reload watcher so agent/file writes do not restart the backend during local development

## Notes

- focused verification passed before release:
  - `.venv/bin/pytest tests/test_auth.py tests/test_chat_validation.py tests/test_skills.py tests/test_memory_pipeline.py tests/test_memory_budget.py tests/test_memory_context.py tests/test_chat_streaming.py -q`
  - `.venv/bin/python -m py_compile app/api/chat.py app/memory/service.py app/agent/tools.py tests/test_auth.py tests/test_memory_pipeline.py tests/test_memory_budget.py`
  - `git diff --check`
- focused result count for the merged chat + memory verification set: `133 passed`
- this is a backend-focused release that intentionally lands the chat metadata primitives ahead of the richer SwiftUI frontend work and lands Memory v2 foundations ahead of nightly extraction/day-4 follow-up work
