# Changelog

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
