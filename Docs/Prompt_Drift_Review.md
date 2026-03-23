# Prompt Drift Review

**Status**: Concept note  
**Phase fit**: Phase 8 follow-on / dream-cycle support  
**Purpose**: Review real execution traces overnight and propose tighter prompts when model behavior drifts from intended tool use or output boundaries.

---

## Summary

Fruitcake already captures enough evidence to see when prompts are under-specified:
- wrong tool chosen
- correct tool with sloppy arguments
- extra output beyond what the user asked for
- repeated confusion between similar tool surfaces
- smaller-model runs that drift further than larger-model runs

The idea is not to let an agent rewrite prompts freely.

The idea is to run a controlled nightly review that:
1. inspects recent run traces
2. detects recurring deviation patterns
3. proposes tighter prompt variants
4. scores whether those variants improve precision enough to justify their token cost

This is a better fit for the dream cycle than for the live request path because prompt tuning should be retrospective, evidence-driven, and reviewable.

---

## Why This Matters

Smaller local models often respond well to more explicit prompts:
- state the exact action
- state the output boundary
- state what not to do

Example:
- weak: `Show me the current workspace directory using the shell`
- tighter: `Use the shell to print the current working directory only. Do not list files.`

The tighter version costs more tokens, but it may produce a cleaner result, especially on smaller models.

That tradeoff should be measured, not guessed.

---

## Proposed Nightly Workflow

1. Collect traces from the previous day
- user prompt
- model used
- tools chosen
- tool arguments
- tool results
- final answer
- latency and token usage if available

2. Detect prompt drift patterns
- wrong tool for the task
- right tool but wrong arguments
- unnecessary extra steps
- output exceeds requested scope
- recurring clarification loops for simple asks
- smaller models underperforming on the same task pattern

3. Generate prompt-tuning suggestions
- diagnosis of the failure mode
- proposed prompt rule
- candidate revised wording
- expected benefit
- expected token/latency cost

4. Evaluate proposals
- replay or sample against similar tasks
- compare:
  - tool precision
  - argument precision
  - response scope discipline
  - token cost
  - latency

5. Produce a review artifact
- nightly report
- candidate prompt deltas
- recommended changes
- confidence and tradeoffs

---

## Guardrails

- Do not silently rewrite core prompts every night.
- Start as a recommendation system, not an autonomous prompt mutator.
- Keep model-specific tuning separate when appropriate.
- Optimize for behavior quality and tool precision, not only success rate.
- Reject prompt changes that improve correctness only by causing unreasonable token bloat.

---

## Good Initial Targets

- workspace vs library tool confusion
- shell overuse when a narrower tool would be better
- failure to respect `only` / `do not`
- document summary requests that pass raw user prose instead of resolved filenames
- smaller-model tool-call precision on operational tasks

---

## Example Output

```md
Observed pattern:
- qwen2.5:14b often uses `ls` when the user asked for current directory only.

Proposed rule:
- For smaller-model shell tasks, explicitly state the exact command objective and output boundary.

Candidate prompt revision:
- "Use the shell to print the current working directory only. Do not list files."

Expected benefit:
- cleaner tool arguments
- less extra output

Tradeoff:
- +10 to +20 prompt tokens
```

---

## Roadmap Fit

This is not a separate live-execution phase.

It fits best as a Phase 8 dream-cycle support capability:
- memory extraction finds what the live agent missed
- prompt drift review finds how the live prompts are underspecified

Together, those form a stronger nightly improvement loop without making the live agent harder to trust.
