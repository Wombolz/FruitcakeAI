# Step-Boundary Runtime Preservation Plan

## Summary

This plan defines the first compaction/preservation pass for Fruitcake task runs.

Chosen defaults:

- tasks only
- configured executors only
- step-boundary preservation only
- no within-step `run_agent` compaction yet

This is a runtime-state formalization pass, not an emergency token-reduction feature.

## Goals

Preserve the executor/runtime context that makes configured executors reliable as the executor system grows.

This pass should carry forward:

- `runtime_contract`
- `current_step`
- `input_summary`
- `persistence_target`
- `active_skills_summary`
- `prior_step_summaries`

It should not introduce within-step history compaction or broader chat compaction.

## Key Changes

### 1. Preserve configured-executor state at step boundaries

Introduce a compact preserved runtime-state structure for configured-executor task runs.

This preserved state should include:

- runtime contract summary
- current step title/instruction/final-step flag
- compact prepared-input summary
- persistence target
- active skill summary
- prior step summaries

### 2. Persist preserved state as a task-run artifact

Add a new task-run artifact:

- `preserved_runtime_state`

This artifact should carry the compact step-boundary state, not full diagnostics or raw dataset blobs.

### 3. Reinject the preserved state into later steps

For configured executors, later steps — especially final synthesis — should receive a compact plain-language preservation block derived from the preserved state.

This block should complement existing prompt assembly rather than replace:

- task instruction
- step instruction
- memory context
- prepared dataset prompt when still required

### 4. Keep scope narrow

Do not change:

- non-configured profiles
- chat compaction
- within-step `run_agent` history compaction
- turn limits
- token-budget logic

## Test / Verification

1. configured-executor runs emit:
   - `prepared_dataset`
   - `runtime_contract`
   - `preserved_runtime_state`
   - `final_output`
   - `validation_report`
   - `run_diagnostics`

2. `preserved_runtime_state` includes:
   - compact contract/state summaries
   - no full raw dataset blob
   - no full diagnostics dump

3. final synthesis prompt includes the preservation block for configured executors

4. task 69 remains valid and backward-compatible

5. non-configured task profiles remain unchanged

## Assumptions

- The branch for this work is `codex/declarative-runtime`.
- The first implementation pass is about runtime-state formalization, not emergency token reduction.
- Within-step compaction remains a later decision after step-boundary preservation proves useful.
