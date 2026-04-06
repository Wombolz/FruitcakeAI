# Briefing Family Runtime Slice

## Summary

Implement the next roadmap slice as a **runtime-first `briefing` family evolution**.

Chosen direction:
- replace the rigid `morning_briefing` mental model with a broader **`briefing` family**
- keep the first implementation focused on **daily-style briefings**, but prove **morning** and **evening** framing from the start
- prioritize **briefing assembly and output quality**
- keep UI/editor work **minimal but compatible**
- allow **light MCP co-evolution** only where it improves visibility into briefing behavior during development

This slice should improve real briefing quality while moving Fruitcake toward the recipe/ingredient model already documented, without turning into a full editor redesign or naming-system rewrite.

## Key Changes

### 1. Introduce a real `briefing` family contract
Create a shared runtime contract for briefings that separates:
- `family: briefing`
- `briefing_mode`: `morning` or `evening`
- structured inputs/ingredients
- optional guidance
- output contract

Keep the first supported ingredient set intentionally small:
- calendar
- RSS/news
- weather
- market data
- optional day-in-history / trivia
- custom guidance

Do not make ingredient selection fully open-ended yet. Use a bounded built-in ingredient set so quality stays controllable.

### 2. Make briefing assembly structured instead of prompt-patched
Refactor briefing generation so the runtime assembles a briefing from explicit structured sections rather than relying on one brittle monolithic prompt.

Implementation shape:
- build a normalized `briefing_config` or equivalent task recipe params object
- derive section inputs from declared ingredients
- generate a predictable section order based on mode
- keep morning/evening differences as framing/config, not separate hardcoded families

Structural interpretation for this slice:
- `source/join`
  - where the inputs come from
  - especially whether the task combines multiple sources such as calendar + RSS
- `reshape`
  - filtering, trimming, source restriction, ingredient gating, dataset normalization, headline limits
- `aggregate`
  - section assembly
  - section order
  - required sections
  - empty-state behavior
  - morning/evening framing
- `effect`
  - return only
  - notify
  - append/write/publish

Important note:
- the current runtime axes remain the operational surface
- this structural layer is meant to clarify how briefing behavior should be reasoned about
- `tool_policy` remains a capability boundary, not one of the four transform classes

Expected v1 mode behavior:
- `morning`
  - emphasize day start, schedule, forecast, market open context, top developments
- `evening`
  - emphasize summary of day, tomorrow-facing prep, closing market/weather context, notable developments

This should replace the current “evening briefing ran but produced a sub-par briefing” problem with a clearer, structured assembly path.

Aggregate contract focus for this slice:
- make required section set explicit
- make section order explicit by `briefing_mode`
- make empty-state section behavior explicit
- make headline count and summary rules explicit
- make ingredient-to-section expectations explicit enough that validation is confirming a visible contract rather than discovering the structure for the first time

### 3. Preserve backward compatibility with current briefing tasks
Existing tasks using current briefing families should continue to work through a compatibility layer.

Required behavior:
- current `morning_briefing` tasks normalize into `briefing` with `briefing_mode: morning`
- existing persisted params continue to load
- malformed legacy briefing-like tasks remain editable/repairable through explicit params
- no silent fallback to generic

Do not do a broad naming migration across all UI/internal labels in this slice. Keep the compatibility bridge and defer the larger naming pass to the broader restructuring phase.

### 4. Keep the task editor compatible, but do not make it ingredient-driven yet
Update the shared task editor only enough to remain correct with the new family.

Include only the minimum needed fields:
- family: `briefing`
- mode: `morning` / `evening`
- topic
- output path
- window
- custom guidance

If useful, expose a simple bounded section/input toggle set for the built-in briefing ingredients, but do not build a full schema-driven ingredient editor yet.

The editor goal in this slice is:
- no regressions
- no hidden required params
- no full dynamic add-on UI yet

### 5. Add briefing-specific inspection via MCP only where it helps the loop
Allow light MCP co-evolution only to make briefing debugging easier.

Bounded additions if needed:
- inspect normalized briefing config
- inspect selected briefing ingredients/sections for a run
- inspect briefing assembly diagnostics or validation findings

Do not grow MCP into a parallel product track here. Add only what materially improves direct validation of briefing quality.

## Public / Interface Changes

Expected task/runtime contract changes:
- new primary recipe family concept: `briefing`
- new briefing field: `briefing_mode`
- existing `morning_briefing` paths normalize into the new family shape
- task editor remains compatible with existing create/edit flow and gains the minimum fields needed to express `briefing` correctly

Possible payload additions:
- normalized briefing config in task recipe metadata
- optional bounded ingredient/section flags in recipe params

Current field interpretation:
- `briefing_mode`
  - aggregate framing input
- `ingredients`
  - mostly reshape/join selectors
- `required_sections`
  - aggregate contract
- `headline_limit`
  - reshape constraint on aggregate output
- `path`
  - effect target
- `custom_guidance`
  - prompt/context modifier, not a primitive class by itself

## Test Plan

1. Backward compatibility
- an existing `morning_briefing` task still runs successfully
- it normalizes to `briefing` + `briefing_mode: morning`
- no silent downgrade to generic

2. New task creation/editing
- create a new morning briefing through the existing editor flow
- create a new evening briefing through the existing editor flow
- reopen and edit both successfully
- required fields remain explicit and save failures remain honest

3. Runtime quality
- morning and evening outputs are meaningfully different in framing
- section ordering is stable and mode-appropriate
- structured ingredients appear in the right sections
- briefings no longer depend on one fragile all-in-one prompt path

4. Legacy repair
- malformed briefing-like tasks such as the prior politics/technology cases can be repaired into the new family using explicit fields
- missing required fields still yield clear validation errors

5. MCP support
- if MCP additions are included, direct inspection can show normalized briefing config and enough assembly detail to debug poor outputs without reading raw logs

## Assumptions

- This slice is **runtime and quality first**, not a full UI/schema refactor.
- The first `briefing` family should prove **morning + evening**, but remain bounded to daily-style briefings.
- Ingredient selection stays built-in and intentionally limited in v1.
- The broader naming pass, fully schema-driven editor behavior, and richer ingredient/add-on UI remain later follow-on work.
- MCP expansion in this slice is justified only when it directly improves briefing debugging or validation.
- Existing runtime axes remain valid; this fold-in is a design-contract clarification, not a stored schema rewrite.
