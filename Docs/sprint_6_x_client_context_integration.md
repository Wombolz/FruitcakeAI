# Sprint 6.x — Client Context Integration (Apple Additive, Platform-Neutral)

## Summary

Add a platform-neutral client context layer that lets Apple clients provide optional launch/context metadata from App Intents, Shortcuts, and similar system surfaces without making Fruitcake Apple-dependent.

Fruitcake core remains platform-neutral and fully functional for Android, web, and other non-Apple clients. Apple is the first producer of optional client context, not the definition of the product.

Recommended roadmap placement:
- Phase 6 optional client enhancement work, after the Phase 5.4 hardening gate
- Framed as a platform-neutral backend contract first used by Apple clients

## Goals

- Add optional `client_context` support to chat-facing backend APIs.
- Merge client-provided context into prompt assembly as transient context only.
- Keep all Apple-specific logic in the Swift client.
- Preserve identical baseline behavior when `client_context` is absent.

## Non-Goals

- No Apple-only backend paths.
- No Spotlight/Core Spotlight indexing in this sprint.
- No Foundation Models offline fallback in this sprint.
- No syncing Apple semantic indexes into Postgres.
- No automatic promotion of client context into long-term memory.

## Implementation Changes

### 1. Extend chat-facing backend APIs

Add optional `client_context` to the REST and WebSocket chat request path.

The contract is intentionally platform-neutral:

- `client_context.platform`: `apple | android | web | unknown`
- `client_context.source`: entry source such as `app_intent`, `spotlight`, `shortcut`, `manual`
- `client_context.user_query`: optional original query when it differs from the visible prompt
- `client_context.selected_entity`: optional `{type, id, title, summary}`
- `client_context.context_summary`: optional short client-supplied summary
- `client_context.capabilities`: optional string list describing client features used to produce context

Rules:

- `client_context` is metadata only, never the source of truth
- requests must behave normally when it is omitted
- malformed or partial client context must degrade gracefully

### 2. Add shared prompt/context assembly

Introduce a shared context-assembly step that merges:

- existing user/persona context
- memory retrieval
- optional `client_context`
- existing library grounding

Behavior:

- inject `client_context` as a labeled transient prompt block
- do not treat `client_context` as durable memory
- do not bypass persona restrictions or blocked tools
- ignore unresolved or unusable client context instead of failing the request

### 3. Bring normal chat up to the same memory baseline as tasks

Normal chat should use the same memory retrieval baseline already used in task execution. This ensures Apple-supplied client context is an additive hint layered onto the same per-user memory system rather than creating a special Apple-only path.

Expected result:

- chat and tasks both reason with user memory
- Apple launch context improves relevance without becoming a requirement

### 4. Resolve selected entities through backend truth when possible

If `client_context.selected_entity.id` maps to a Fruitcake record, resolve it through backend data before using it heavily in prompt construction.

Examples:

- selected document
- selected task
- selected chat session

If the entity cannot be resolved:

- continue normally
- treat the client payload as optional context only

## Acceptance Criteria

- Requests without `client_context` behave exactly as before.
- Apple clients can pass optional context without changing core behavior.
- Android, web, and other clients remain first-class and can omit the field entirely.
- Invalid or partial `client_context` is ignored safely.
- Memory retrieval works in normal chat as well as tasks after the refactor.
- Persona-blocked tools remain blocked even when `client_context` suggests a relevant action.

## Test Plan

- REST chat without `client_context`
- REST chat with Apple `client_context`
- WebSocket chat with Apple `client_context`
- unresolved `selected_entity`
- persona-blocked tools with relevant client context present
- regression coverage for existing chat and task behavior

## Positioning Note

This sprint is an additive client enhancement.

It must not be described as "Apple Intelligence integration" in a way that implies Apple dependency at the core product level.

Preferred wording:

> Platform-neutral client context layer, first used by Apple clients.

## Assumptions And Defaults

- Filename: `Docs/sprint_6_x_client_context_integration.md`
- Roadmap placement: Phase 6 optional client enhancement work
- Framing: Apple is the first producer of optional client context; Android-equivalent integrations can adopt the same backend contract later
