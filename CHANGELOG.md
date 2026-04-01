# Changelog

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
