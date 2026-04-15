# RSS Chat Convergence And Headline Roundup Plan

## Summary
Stabilize the general RSS chat pipeline so feed-backed chat stops over-researching and converges on an answer once it has enough evidence.

This is not just a timeline-query bug. We observed the same expanding-loop behavior on:
- chronology prompts such as "how did this evolve over the weekend?"
- headline roundup prompts such as "what are the headlines this evening?"

So the next slice should treat this as a shared RSS chat convergence problem, not a one-off query special case.

## Problem Statement
The current RSS stack is improved but still incomplete:
- query parsing and ranking are better
- timeline retrieval exists
- repeated semantic RSS re-search loops are now stopped
- broken tool-call leakage is now validated out
- the `RSS News Research` skill exists and is installed

But the system still lacks a strong answer-convergence layer.

The main remaining weakness is:
- the model keeps trying to improve retrieval after it already has enough RSS evidence
- when the guardrail stops it, it often returns a stop diagnostic instead of synthesizing from gathered evidence

## Goals
- make RSS-backed chat recognize when it already has enough feed evidence to answer
- improve "headline roundup" behavior for recent-news prompts
- make no-progress RSS research fall through to synthesis whenever possible
- keep this work scoped to RSS chat behavior, not broad agent/runtime changes

## Key Changes

### 1. Add RSS answer-convergence rules after retrieval
Introduce a shared post-retrieval decision layer for RSS chat:
- inspect recent RSS tool outputs already gathered in the turn history
- determine whether they already support an answer
- if yes, stop research and synthesize from those results

This should trigger before:
- another repeated RSS search
- another limit expansion on the same query/window

## 2. Add a direct headline-roundup path
Add a dedicated path for prompts like:
- "what are the headlines this evening?"
- "what are tonight's headlines?"
- "what's new right now?"

Behavior:
- prefer a recent-items or top-ranked recent RSS set first
- do not start with repeated topical probing unless the prompt is explicitly topic-scoped
- summarize the strongest current items directly

This should be treated as distinct from chronology/timeline retrieval.

### 3. Improve no-progress fallback behavior
When RSS guardrails detect repeated semantic search on the same query/window:
- do not default straight to a user-facing stop diagnostic
- first attempt one synthesis pass from the strongest already-retrieved RSS evidence
- only emit the stop/diagnostic message if there truly is not enough usable evidence to answer

This is the biggest product-quality gap left in the RSS chat path.

### 4. Tighten "enough evidence" heuristics
Define lightweight heuristics for when the system should stop searching and answer:
- enough clearly relevant items were retrieved
- retrieved items already span the requested period
- further searches would only widen limits without improving topic precision
- recent headline prompts already have a usable current set

Keep this deterministic and shallow. No extra semantic planner is needed for v1.

### 5. Keep skill and runtime roles separate
Preserve the current layering:
- core/runtime handles:
  - loop stopping
  - validation
  - fallback-to-synthesis behavior
- the RSS skill handles:
  - query strategy
  - preferred tool use
  - answer shape

Do not push all convergence logic into the skill alone.

## Public Interfaces / Types
Potentially extend existing internal chat/RSS helper logic only.

No schema change is required.
No new top-level UI surface is required.

Optional additions:
- a small internal helper for "headline roundup" classification
- a structured internal RSS evidence summary object for synthesis fallback

## Test Plan

Add focused cases for:

- chronology prompt:
  - weekend evolution query retrieves once or twice, then synthesizes
- headline roundup prompt:
  - "what are the headlines this evening?" uses a recent-headlines path and answers without repeated search escalation
- no-progress fallback:
  - repeated semantic RSS search attempts trigger synthesis from prior RSS evidence instead of only returning a stop message
- thin evidence case:
  - if evidence is genuinely thin, the system says so clearly rather than hallucinating completeness
- skill interaction:
  - the installed `rss-news-research` skill continues to guide query strategy but does not replace core convergence behavior

## Assumptions
- This is a post-branch bug/hardening slice, not more work for the current compaction branch.
- The core bug is answer convergence, not raw RSS availability.
- Timeline and headline-roundup prompts should share the same convergence philosophy but not necessarily the same retrieval path.
