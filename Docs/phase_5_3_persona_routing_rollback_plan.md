## Phase 5.3 Plan - Roll Back News-Specific Runner Logic and Add Task Persona Routing by Intent

### Summary
Remove brittle, domain-specific "news mode" behavior from `TaskRunner`, and replace it with a generic, configurable persona-routing system that assigns a persona to each task. Persona selection is intent-based at task creation/update, explicit persona assignment is supported on the task API, and existing tasks are lazily backfilled on next run.

This keeps orchestration generic while preserving flexibility and improving predictability through stable per-task persona assignment.

---

## Goals and Success Criteria

### Goals
1. Remove hard-coded news instructions from execution layer.
2. Allow `Task` to carry its own persona (`task.persona`) independent of user default.
3. Add intent-based persona inference using config (`personas.yaml`) instead of runner conditionals.
4. Ensure explicit persona on task always wins.
5. Lazy-backfill existing tasks with inferred persona on next run.

### Success Criteria
1. No news/headline-specific branches remain in `TaskRunner`.
2. Task create/patch/get/list support persona field.
3. TaskRunner executes with `task.persona` when present, otherwise infers once and persists.
4. Existing tasks with null persona get inferred/persisted at first run.
5. Current tests pass + new routing tests pass.

---

## Scope

### In Scope (this sprint)
- Backend model, migration, API, runner, persona config, and tests.
- Rollback of recently added news-mode and sequentialthinking suppression logic from runner.
- Intent routing rules stored in `config/personas.yaml`.

### Out of Scope
- Swift UI changes (persona picker/display) for now.
- LLM-based persona classification.
- Reworking task planner semantics.

---

## Decisions Locked

1. Intent routing: **Auto on create/update**.
2. Fallback persona: **`family_assistant`**.
3. Task API: **Add first-class `task.persona` field**.
4. Routing rule storage: **`personas.yaml`**.
5. Override policy: **Explicit task persona wins**.
6. Existing tasks: **Lazy backfill on next run**.
7. Delivery scope now: **Backend only**.

---

## Implementation Design

### 1) Roll Back News-Specific Runner Logic
Files:
- `/Users/jwomble/Development/fruitcake_v5/app/autonomy/runner.py`
- `/Users/jwomble/Development/fruitcake_v5/tests/test_task_steps.py`

Remove:
- `_is_news_headlines_task(...)`
- `_news_workflow_instructions(...)`
- `news_mode` plumbing in `_execute_agent` and `_execute_planned_steps`
- prompt injection of news workflow instructions
- forced `sequentialthinking` append to blocked tools in runner

Keep:
- the non-news improvements you still want:
  - final planned output should use final/full step result (not concatenated 240-char summaries)
  - recurring step snapshot retention in `TaskRun.summary` before reset

Test updates:
- Remove/replace news-policy-specific tests.
- Keep tests for final output and snapshot retention.

---

### 2) Add `persona` to Task Model + Migration
Files:
- `/Users/jwomble/Development/fruitcake_v5/app/db/models.py`
- `/Users/jwomble/Development/fruitcake_v5/app/db/migrations/versions/<new_revision>.py`

Changes:
- Add nullable column on `Task`:
  - `persona = Column(String(100), nullable=True, index=True)` (index optional but recommended for admin filters later).
- Migration:
  - Add `tasks.persona` column nullable.
  - Do **not** backfill in migration (lazy backfill is runtime behavior).

Compatibility:
- Existing rows remain null until assigned/inferred.

---

### 3) Extend Task API Contract for Persona
Files:
- `/Users/jwomble/Development/fruitcake_v5/app/api/tasks.py`

Schema changes:
- `TaskCreate`: add `persona: Optional[str] = None`
- `TaskPatch`: add `persona: Optional[str] = None`
- `TaskOut`: add `persona: Optional[str]`

Validation:
- If `persona` provided on create/patch, validate via `persona_exists(...)`; return `400` with available personas on invalid value.
- If omitted:
  - create: infer persona from title+instruction and persist
  - patch:
    - if title/instruction changed and persona not explicitly set, re-infer and update
    - if persona explicitly set, keep as-is

`_to_task_out(...)`:
- include persona.

---

### 4) Add Intent Router (Config-Driven)
New file:
- `/Users/jwomble/Development/fruitcake_v5/app/agent/persona_router.py`

Responsibilities:
- `infer_persona_for_task(title: str, instruction: str) -> tuple[str, float, str]` (persona, score, reason)
- Deterministic keyword scoring (no LLM call).

Rule source:
- Extend `/Users/jwomble/Development/fruitcake_v5/config/personas.yaml` with optional routing metadata per persona, e.g.:
  - `intent_keywords: [flight, hotel, itinerary, headline, stocks, ...]`
  - `intent_phrases: [...]` (optional)
- Matching strategy:
  - normalize lowercase, token boundary matches for keywords, simple substring for phrases.
  - weighted score by keyword hits.
  - tie-break: highest score, then stable persona order from config.
  - threshold: if no meaningful match, fallback to `family_assistant`.

Initial config:
- Add at least one new persona for your goal (example: `news_researcher`) with:
  - description/tone
  - blocked tools as desired
  - intent keywords

Note:
- Keep existing personas valid even without routing metadata.

---

### 5) Use `task.persona` in Runner + Lazy Backfill
Files:
- `/Users/jwomble/Development/fruitcake_v5/app/autonomy/runner.py`

Behavior:
1. Load task and user.
2. Determine execution persona:
   - if `task.persona` set -> use it (explicit wins).
   - else infer using router from `task.title + task.instruction`, persist to `task.persona` (lazy backfill), and use it.
   - if inferred persona invalid/missing due to config drift -> fallback `family_assistant` and persist fallback.
3. Use resolved persona for:
   - `ChatSession.persona`
   - `UserContext.from_user(..., persona_name=resolved_persona)`

No domain-specific prompt policy injection in runner.

---

### 6) Optional API Ergonomics (within backend scope)
File:
- `/Users/jwomble/Development/fruitcake_v5/app/chat` or task service (if needed)

Add non-breaking behavior:
- If chat/tool-created task creation path exists, ensure it can pass `persona` or receive inferred one consistently with `/tasks` API.

---

## Public Interface Changes

### `/tasks` and `/tasks/{id}` (response)
- Add:
  - `persona: string | null`

### `POST /tasks` request
- Add optional:
  - `persona: string | null`
- Server behavior:
  - if null/omitted -> infer and store
  - if provided -> validate and store

### `PATCH /tasks/{id}` request
- Add optional:
  - `persona: string | null`
- Server behavior:
  - explicit value wins
  - if omitted and intent text changed, re-infer

No breaking removals.

---

## Test Plan

### Unit tests
1. `persona_router` keyword/phrase matching.
2. tie-break + fallback to `family_assistant`.
3. invalid config/persona resolution fallback behavior.

### API tests (`tests/test_task_steps.py` or dedicated task API tests)
1. create task with explicit persona stores and returns persona.
2. create task without persona infers and returns persona.
3. patch task persona explicit override works.
4. patch title/instruction without explicit persona re-infers.
5. invalid persona returns 400 with list of valid personas.

### Runner tests
1. runner uses explicit `task.persona` (no inference path).
2. runner lazy-backfills null persona and persists inferred value.
3. runner uses `family_assistant` fallback when no match.
4. no news-specific instruction injection appears in prompts.
5. planned-output and recurring-snapshot behaviors remain passing.

### Regression tests
- Remove the test asserting news-mode prompt injection and forced sequentialthinking block.
- Keep/adjust final-output and recurring snapshot tests to ensure those improvements remain.

---

## Rollout Steps

1. Add migration for `tasks.persona`.
2. Deploy backend.
3. Run `alembic upgrade head`.
4. Restart app workers.
5. Verify:
   - create task without persona => inferred persona appears in `/tasks`.
   - existing null-persona task acquires persona after next run.
   - task run session uses `ChatSession.persona == task.persona`.
6. Monitor `/admin/audit` and `/admin/task-runs` for improved consistency and no news-mode artifacts.

---

## Assumptions and Defaults

1. Persona routing is deterministic and config-driven (no classifier model call).
2. `family_assistant` always exists and is the global fallback.
3. Intent inference is based on `title + instruction` only.
4. Explicit task persona is authoritative and never overwritten automatically.
5. UI integration is deferred to a subsequent sprint.
