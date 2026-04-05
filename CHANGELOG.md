# Changelog

## v0.7.11

Release date:
- 2026-04-05

This release turns the new Fruitcake MCP surface into a practical developer-loop and operator inspection tool: Codex can now inspect real task/library/runtime state directly, and aggregate task health rollups make recurring failures, cancellation churn, and suspicious memory-candidate contradictions easier to spot.

### Added

- bounded authenticated Fruitcake MCP HTTP surface for:
  - task list / get / draft / create / update
  - library list / search / summarize
  - scheduler health and recent task runs
- higher-level run inspection tools with summarized artifacts, structured content, and memory-candidate visibility
- aggregate task health rollups with success/failure/cancellation summaries over a selected time window
- MCP-friendly rollup window aliases like `24h`, `7d`, and `rollup_7d`
- direct inspection signals for repeated errors, no-artifact failed/cancelled runs, and aggregate contradiction-style memory candidate findings

### Fixed

- direct Codex MCP testing no longer requires stitching raw artifacts together just to understand task health
- rollup windows now honor friendly live MCP values instead of silently collapsing back to the default 24-hour window
- aggregate rollups now surface contradiction-style memory candidate issues that were previously only visible in single-run inspection

### Operator Notes

- this slice is primarily enabling infrastructure for the development loop; it shortens diagnosis time while the roadmap returns to product/runtime work
- the MCP surface is also the early shape of a future admin/control plane, but it should remain bounded and expanded only when it materially improves testing or operator visibility

## v0.7.10

Release date:
- 2026-04-05

This release lands the first usable task creator/editor flow: chat can propose task drafts, the client can open a native editor for review before save, and existing tasks can reopen in the same shared editor for correction.

### Added

- `TaskDraftPayload`-based draft handoff so chat can propose task creation without persisting immediately
- native task editor popup flow for chat-created drafts with family-aware review before save
- shared create/edit task editor support so existing tasks can reopen in the same focused native surface
- briefing-specific and watcher-specific editor fields with structured recipe params instead of relying only on prose inference
- read-only task recipe and assumptions visibility in task detail/editor flows

### Fixed

- explicit family selections now fail honestly instead of silently downgrading malformed tasks back to generic
- one-shot tasks can now be edited into recurring tasks and keep the chosen schedule after save
- `/tasks` create/update paths now persist editor-selected recipe family and params correctly
- websocket task-draft handoff is more resilient to partial payloads and date decoding drift on the client side
- task rows/detail surfaces now show clearer family and schedule identity for easier scanning and review

### Operator Notes

- this release materially improves the chat-to-task and post-creation correction loop, but some v1 families like `maintenance` remain intentionally rigid until the later recipe/ingredient generalization work
- older malformed tasks created before the editor flow may still need structured repair through the new editor or later cleanup work

## v0.7.9

Release date:
- 2026-04-03

This release fixes the recurring task claim race that could cause a task to complete and then start again immediately from a stale queued dispatch.

### Fixed

- recurring task claims now handshake against the queued `next_run_at` value instead of only `status = pending`
- stale queued dispatches are skipped cleanly after a successful recurring run reschedules the task into the future
- manual-run and scheduler-adjacent runner paths retain compatibility while using the stronger claim semantics
- focused regression coverage now protects the stale queued-dispatch case directly

### Operator Notes

- this is a backend execution fix; restart the server after deploy so the updated runner claim logic is active

## v0.7.8

Release date:
- 2026-04-02

This release lands the first chat task runtime groundwork: recipe-backed task normalization, stronger confirmation text, broader daily briefing inference, and safer task-family switching ahead of the dedicated task creator/editor sprint.

### Added

- recipe-backed task normalization and persisted `task_recipe` metadata for supported chat-created task families
- stronger task confirmation summaries and handoff text for chat-created tasks
- broader recurring daily briefing inference coverage for:
  - spaced workspace paths
  - `previous 24 hours` phrasing
  - `summary` / `analysis` / `briefing` title variants
- focused regression coverage for briefing creation, planner setup-step filtering, and recipe-family switching

### Fixed

- daily briefing requests are less likely to fall through to generic planning or watcher-shaped tasks
- planner-generated task steps no longer include redundant setup/reminder steps for already-created recurring tasks
- switching a task from one recipe family to another now clears stale profile carryover correctly
- watcher recipe source handling remains hardened when chat suggests brittle or unmapped source restrictions

### Operator Notes

- this release improves chat-to-task runtime reliability but does not finish the task creator/editor UX
- existing malformed tasks created before these fixes may still need manual repair or recreation

## v0.7.7

Release date:
- 2026-04-02

This release closes the long-running time semantics thread by standardizing timezone precedence, keeping UTC as the storage truth, and normalizing the highest-value human-facing schedule and profile outputs.

### Added

- shared timezone helper coverage for effective timezone resolution, localized display formatting, and paired local/UTC rendering
- additive task API fields for localized schedule metadata:
  - `effective_timezone`
  - `created_at_localized`
  - `last_run_at_localized`
  - `next_run_at_localized`
- focused regression coverage for:
  - timezone precedence
  - DST-safe recurring scheduling
  - scheduler user-timezone fallback
  - ISS/weather display-time formatting

### Fixed

- recurring backlog skipping now uses the canonical task scheduling path instead of bypassing task/user timezone resolution
- ISS and weather profiles no longer hardcode `America/New_York` in user-facing output when a task-specific timezone is available
- task API schedule surfaces now present localized companion timestamps without changing stored UTC values
- invalid timezone names now fall back cleanly to UTC through the shared helper path

### Operator Notes

- UTC remains the storage and scheduler execution truth
- task `active_hours_tz` overrides user timezone, user timezone overrides UTC fallback
- durable IDs, filenames, and low-level metadata remain UTC-safe even when display timestamps are localized

## v0.7.6

Release date:
- 2026-04-01

This release lands the first bounded JSON/API integration slice, proves it on ISS and weather, and hardens the chat/runtime path around live integration failures.

### Added

- deterministic `response_fields` extraction for backend-owned JSON/API calls
- OpenWeather-backed `weather + current_conditions` integration and `weather_conditions` task profile
- structured ISS and weather profile contracts on top of the JSON/API substrate

### Fixed

- weather secret handling now tolerates provider-label drift while still enforcing approved secret names
- chat `api_request` calls now lift top-level tool-call fields into `query_params` when the model omits the nested object
- weather requests now accept both `latitude`/`longitude` and `lat`/`lon`, and honor requested `units`
- RSS refresh no longer fails on duplicate items within the same fetched feed batch

### Operator Notes

- the live weather rollout confirmed that tool-argument normalization is part of the runtime contract surface, not just adapter logic
- future backend-owned integrations should assume chat may emit a flatter tool-call shape and rely on the normalization layer before backend validation

## v0.7.5

Release date:
- 2026-04-01

This release hardens Fruitcake's secrets layer, adds admin/operator visibility into secret access, and fixes the ISS watcher's secret-decryption and fallback handling.

### Added

- explicit `SECRETS_MASTER_KEY` enforcement for backend secret resolution
- owner-facing secret access history
- admin-facing secret access audit visibility
- clearer failure reporting for secret decryption problems

### Fixed

- secret rotation now cleanly recovers previously encrypted credentials when the master key is stable
- ISS watcher runs no longer hide secret decryption failures behind a generic `API request failed.` message
- strict API-only task profiles now opt out of irrelevant skill injection

### Operator Notes

- if a secret was encrypted under a different master key, rotate or re-save it after updating `.env`
- the broader JSON/API integration branch can now build on a real secret store instead of `.env`-only credentials

## v0.7.4

Release date:
- 2026-03-31

This release lands the first declarative-runtime preservation and repetitive-reporting hardening pass for configured research briefing tasks.

### Added

- step-boundary `preserved_runtime_state` artifacts for configured executors
- compact runtime-contract reinjection for later configured-executor steps
- conservative repetitive-reporting dataset shaping:
  - recent-entry exact-repeat trimming
  - light title-cluster diversity
- integration coverage for configured-executor dataset preparation behavior

### Fixed

- configured-executor runs now reject overlapping active task runs more consistently
- manual task runs and `run_task_now` now honor active `TaskRun` records instead of relying only on task status
- prior-step carry-forward is more compact and less noisy
- non-final configured-executor steps no longer use the full final-briefing contract
- final configured-executor steps no longer ingest prior full step outputs in a way that can corrupt validation
- repetitive reporting output now suppresses near-identical consecutive file appends while preserving a successful run outcome

### Operator Notes

- repetitive reporting tasks now use a conservative two-layer dedup approach:
  - persistence-time duplicate suppression
  - pre-draft dataset shaping for very recent repeats
- broader story-cluster diversity and stronger novelty scoring remain future quality work, not part of this release

## v0.7.3

Release date:
- 2026-03-31

This release lands the first configured research-briefing executor path and the supporting task/runtime fixes around it.

### Added

- configured executor v1 for daily research briefings
- deterministic configured-executor planning for task-69-style recurring research tasks
- workspace append persistence path for recurring briefing output

### Fixed

- task scheduling now respects task/user timezone intent when computing future runs
- recurring run rescheduling uses the same timezone-aware task scheduling path
- configured-briefing validation now tolerates a heading before the bullet block
- configured-briefing output strips watcher-style memory-candidate sections before persisting to workspace files
- runner regressions around user loading, artifact persistence ordering, and recurring reschedule helper usage

### Operator Notes

- existing tasks keep their stored `next_run_at` until recomputed or updated once under the new time-semantics rules
- task `69` was migrated onto the configured executor path and validated against its report-file output

## v0.7.2

Release date:
- 2026-03-30

This is the current public alpha candidate release.

### Added

- persistent manual chat session ordering
- visible reorder controls in the native chat sidebar
- message footer timestamps now include both date and time
- public `CONTRIBUTING.md`, `SUPPORT.md`, and GitHub issue templates
- backend CI workflow for compile checks and focused smoke tests

### Fixed

- websocket stale-payload replay after completed chat turns
- duplicate post-completion websocket handling in chat
- calendar replies no longer echo raw event IDs back into the final chat response
- session ordering persistence now survives reloads through database-backed `sort_order`

### Operator Notes

- run migrations before upgrading a live backend
- this remains an alpha-stage release: breaking changes and schema movement are still possible
- FruitcakeAI is still intended for trusted-network, operator-controlled deployments unless you add your own hardening layers
