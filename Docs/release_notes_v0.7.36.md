# Release Notes v0.7.36

## Summary

This release unifies Fruitcake's chat execution plumbing and adds durable, inspectable chat runs. Streaming and collected responses now share one agent loop, while operators can diagnose each run by phase, tool activity, retries, latency, grounding outcome, token use, and prompt-cache behavior.

## Included Changes

- introduced typed, ordered lifecycle events for agent execution
- unified streaming and non-streaming turns around shared preparation, tool dispatch, convergence, and finalization behavior
- centralized model-provider capability decisions for Ollama, OpenAI, and compatible providers
- normalized structured tool results, artifacts, citations, error state, and approval state without changing provider-facing tool messages
- added durable chat-run IDs, status, phases, model/mode/stage context, terminal classification, and message references
- added exact persisted approval-call replay with approve and deny handling against the original run
- added bounded lifecycle-event traces and chat-run-linked LLM usage records
- persisted cached prompt tokens and available Ollama load, prompt-evaluation, generation, and total timing metrics
- added admin chat-run inspection and the user-scoped `fruitcake_inspect_chat_run` MCP tool
- added deterministic trace qualification coverage for representative Ollama and OpenAI tool-augmented runs

## Compatibility And Privacy

- REST and WebSocket changes are additive; existing response fields remain intact
- chat approval mode remains opt-in until the client adds a visible approval interaction
- existing task-run inspection and MCP tools are unchanged
- traces store operational metadata only: prompts, search-query values, raw tool arguments and results, generated token deltas, and reasoning content are excluded
- deleting a chat session cascades its durable runs and trace events

## Database Changes

- migration `043_chat_runs` adds durable chat-run state
- migration `044_chat_run_traces` adds bounded lifecycle traces and chat-run usage correlation
- normal startup migration must complete before using the new run inspection surfaces

## Verification

- focused lifecycle, approval, structured-result, provider, inspection, and usage suites passed throughout the six implementation slices
- final runtime-event, streaming, chat, authentication, validation, MCP, and usage regression pass: 195 passed with three unrelated stale assertions deselected
- Alembic reports one head at `044_chat_run_traces`
- Python compilation and `git diff --check` passed
