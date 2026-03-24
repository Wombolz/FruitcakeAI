# RSS Newspaper Example

## Summary

`rss_newspaper` is the clearest built-in example of how to configure a structured recurring task in Fruitcake without pushing task-specific logic into the shared chat or runner core.

It demonstrates the main pattern:
- keep the shared task runner generic
- keep task-specific prompt contract in a profile-owned spec file
- keep task-specific validation and export behavior in the profile code
- keep persistence for task-specific freshness/history in the task pipeline, not in the generic RSS system

This document is intended as a reference for future built-in profiles and for understanding how Fruitcake configuration should be shaped.

---

## What RSS Newspaper Uses

The RSS Newspaper capability is composed of five parts:

1. Profile identity and resolution
- canonical profile name:
  - `rss_newspaper`
- backward-compatible alias:
  - `news_magazine`
- resolver:
  - [resolver.py](/Users/jwomble/Development/fruitcake_v5/app/autonomy/profiles/resolver.py)

2. Profile-owned prompt contract
- bundled spec file:
  - [rss_newspaper.md](/Users/jwomble/Development/fruitcake_v5/app/autonomy/profiles/specs/rss_newspaper.md)
- loader:
  - [spec_loader.py](/Users/jwomble/Development/fruitcake_v5/app/autonomy/profiles/spec_loader.py)

3. Profile execution and validation
- profile implementation:
  - [news_magazine.py](/Users/jwomble/Development/fruitcake_v5/app/autonomy/profiles/news_magazine.py)
- responsibilities:
  - deterministic step planning
  - blocked-tool rules
  - prompt assembly
  - output grounding
  - malformed headline rejection
  - final selection and section balancing
  - export behavior

4. Dataset preparation and freshness policy
- dataset builder:
  - [magazine_pipeline.py](/Users/jwomble/Development/fruitcake_v5/app/autonomy/magazine_pipeline.py)
- responsibilities:
  - refresh RSS cache
  - score and section items
  - mark previously published URLs per task
  - provide freshness counts for diagnostics

5. Publication history persistence
- model:
  - [models.py](/Users/jwomble/Development/fruitcake_v5/app/db/models.py)
- migration:
  - [022_rss_published_items.py](/Users/jwomble/Development/fruitcake_v5/app/db/migrations/versions/022_rss_published_items.py)
- purpose:
  - record which RSS items a given task has already published
  - prefer unseen URLs in future runs
  - allow reuse fallback if the pool is too thin

---

## Configuration Pattern

### 1. Use a task profile, not core special-casing

RSS Newspaper is configured as a task profile, not as logic embedded in:
- chat
- the generic task runner
- skills alone

That is the right pattern when a task has:
- a known output contract
- repeatable structure
- special validation rules
- export behavior

### 2. Keep the prompt contract external

Prompt-facing instructions belong in the spec file, not inline in the Python profile.

That keeps:
- wording easy to tune
- orchestration code smaller
- the boundary between prompt contract and enforcement clearer

For RSS Newspaper, the spec owns:
- task purpose
- dataset-only rule
- formatting requirements
- publishability expectations

### 3. Keep enforcement in Python

The spec file is not executable policy.

Python profile code still owns:
- URL grounding and repair
- dedupe behavior
- malformed title rejection
- story and section floors
- artifact/export generation
- persistence hooks

That is important because these are correctness and safety rules, not just prompt preferences.

### 4. Treat model output as assistive, not authoritative

RSS Newspaper uses the prepared dataset as the source of truth.

The model can influence:
- ordering
- summarization
- structure
- emphasis

But the pipeline still prefers:
- dataset-backed URLs
- dataset-backed titles when the model title is malformed
- dataset balancing when the draft is thin

That is the right pattern for any Fruitcake task built on grounded source material.

### 5. Keep freshness local to the task

Publication history is scoped per task.

That means:
- Task 48 can avoid repeating its own articles
- generic RSS search is unchanged
- other tasks are not globally suppressed

This is the right default when freshness is a property of the output format, not of the raw source data.

---

## Current RSS Newspaper Behavior

### Step plan

The profile defines a deterministic two-step plan:
1. `Draft Magazine from Dataset`
2. `Final Dedupe and Publish`

This is profile-owned because the steps are part of the task contract.

### Tool restrictions

RSS Newspaper blocks retrieval-style tools during execution so the task stays constrained to the prepared dataset.

This is profile-owned because it is a capability boundary for this task type, not a global runner rule.

### Headline sanitation

The profile rejects placeholder titles such as:
- `Top Stories`
- `World`
- `Politics`
- `Business`
- `Tech`
- `Science`
- `Culture`
- `Other`
- `Read Next`

When that happens, it falls back to the dataset article title.

This is the right pattern for profile-level cleanup:
- detect a task-specific malformed output shape
- repair it using grounded source data

### Selection and freshness

Final article selection now follows this shape:
- prefer unseen items for the same task
- keep section diversity
- keep source balancing
- use model-mentioned URLs as a bonus, not the whole ranking
- fall back to reused items only if unseen items are insufficient

This is the right Fruitcake pattern for a recurring grounded task:
- fresh-first
- source-aware
- diversity-aware
- model-assisted

### Diagnostics

RSS Newspaper validation now emits freshness diagnostics including:
- `selected_unseen_count`
- `selected_reused_count`
- `reuse_fallback_triggered`
- `unseen_candidate_count`
- `previously_published_candidate_count`

That makes the selection behavior inspectable without reading code.

---

## Why This Is A Good Example

RSS Newspaper is a useful reference because it exercises several Fruitcake patterns at once:
- recurring task scheduling
- profile-based planning
- profile-owned prompt contract
- dataset preparation
- grounded output validation
- export generation
- model routing
- per-task freshness memory

That makes it a good example of how to add structured first-party capabilities without bloating the shared runtime.

---

## Rules For Future Built-In Profiles

Use RSS Newspaper as the reference shape:

1. Put task identity and compatibility rules in the profile resolver.
2. Put prompt contract text in a bundled profile spec file.
3. Put validation and export behavior in profile code.
4. Put task-specific persistence in the relevant task pipeline, not the shared core.
5. Keep the generic runner generic.
6. Prefer grounded source data over model improvisation when correctness matters.

If a new capability does not need validation, export, or structured planning, it may not need a dedicated profile.

If it does need those things, it should follow the RSS Newspaper pattern instead of adding more task-specific conditionals to shared runtime code.
