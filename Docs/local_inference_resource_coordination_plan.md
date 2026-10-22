# Local Inference Resource Coordination Sprint

Recorded October 22, 2026. Planned follow-up only; no implementation or runtime
configuration change is authorized by this document.

## Goal And Sequence

Avoid loading a large Ollama model and a memory-heavy ImageLab workflow together
on the same Mac. Preserve the active conversation while exchanging model residency
between reasoning and image generation, rather than paying for sustained swapping.

The operator reports more than 34 GB of swap during concurrent Qwen/image loading,
and that model reload is cheaper than the resulting rendering slowdown. Treat that
as an observed symptom, not a measured memory requirement or benchmark result.

Close and briefly soak the current per-user ImageLab account, Skills bundle/GUI,
and image-card/private-preview work first. Settle the paired preview contract and
record any remaining provider dependency explicitly in closeout. Then open a
separate `codex/local-inference-resource-coordination` branch from the landed
backend baseline, with a coordinated ImageLab branch if needed. Do not expand the
current branch or automatically merge, restart services, or change `.env`.

This is the next bounded reliability/performance sprint after current closeout.
My Workspace CRUD/basic editing remains the next feature sprint; this prerequisite
does not reopen persona work, coding orchestration, or general scheduling.

## Existing Foundation

- Fruitcake awaits tool execution between inference requests and keeps the message
  history/tool result outside Ollama in `app/agent/core.py`. Unloading between
  requests loses reusable model/KV cache, not the application-held conversation.
  Reload and prompt reevaluation must be included in the performance comparison.
- `_litellm_kwargs()` applies `LOCAL_MODEL_KEEP_ALIVE` (code default `15m`), with
  per-model profile overrides. Incognito and other runtimes need explicit audit;
  do not assume this setting controls every local inference client.
- `app/agent/local_model_lifecycle.py` already requests Ollama unload on backend
  shutdown. It tracks historical use, not active consumers; it is not a lease
  manager and must not be reused to unload every tracked model before a tool.
- ImageLab owns its workflow/backend lifecycle. Its currently verified generation
  and preview contracts do not establish a coordinated model-release guarantee.

Reference behavior: [Ollama keep-alive](https://docs.ollama.com/faq#how-do-i-keep-a-model-loaded-in-memory-or-make-it-unload-immediately)
and [tool-call continuation](https://docs.ollama.com/capabilities/tool-calling).

## First-Sprint Scope

Deliver one-host cooperative admission and a verified Ollama/ImageLab handoff.
Use a resource-group identity for the physical host/memory pool, distinct from
provider URL or user identity. Two services on the same Mac compete for the same
memory; a genuinely remote image backend must not evict the local chat model.

Prefer a conservative exclusive heavy-work lease for the first release rather
than guessed byte-level capacity planning. Ordinary CPU/IO tools need no lease.
Do not force all local workloads into one model or reduce image quality by default.

## Resource Handoff

```text
LLM inference finishes and the complete tool call is retained
  -> admit image work; block new competing heavy inference on that host
  -> drain existing inference consumers; release idle competing Ollama models
  -> verify release before admitting image model loading
  -> ImageLab generates and retains the result exactly once
  -> ImageLab releases workflow models and confirms backend idle/released
  -> release the image lease; resume queued LLM work
  -> reload the selected model and continue with the existing tool result
```

An active Fruitcake turn is not an active Ollama inference request. Waiting on a
tool is a valid handoff boundary; streaming tokens or a still-open provider request
is not. Keep complete assistant/tool-call messages, identifiers, and any required
thinking fields according to the existing provider contract; never rebuild a turn
from a prose summary just because its model was unloaded.

## Delivery Slices

### RC0: Measure And Agree On Ownership

1. Trace ordinary Chat, scheduled tasks, coding/Pi and local helper-model calls.
   Identify which actually use this Ollama server and which bypass Fruitcake's
   provider wrapper. Name uncovered callers rather than claiming global coverage.
2. Measure a representative operator-approved image request: inference completion,
   Ollama residency, image load/render/release, LLM reload/prompt evaluation, total
   latency and OS memory pressure/swap activity. Avoid deliberately recreating
   severe swapping just to obtain a baseline; stop on sustained pressure.
3. Verify ImageLab/ComfyUI's actual release capability and whether image models
   remain resident after success, failure and cancellation. Neither a completed
   tool response nor an empty job queue proves model memory has been released.
4. Agree on backend host grouping, lifecycle ownership, the local lease authority
   and external-client limitations before implementing admission.

Gate: a documented, testable release/idle acknowledgement exists on both sides.
If not, deliver the narrow provider capability first; do not label a timer a
coordinated handoff.

### RC1: Provider Lifecycle And ImageLab Coordination

- Add a Fruitcake-owned internal lifecycle adapter for observing model residency,
  releasing eligible idle models and reporting confirmation/failure. Ollama's
  empty `keep_alive: 0` request is an adapter implementation detail.
- ImageLab must own safe workflow release and synchronize it with its native GUI
  jobs and any other MCP callers. Fruitcake must not call an exposed ComfyUI
  administrative endpoint directly or kill a provider process.
- Write and approve a paired lifecycle contract covering admission, host identity,
  active work, release confirmation, cancellation and failure. Exact method names
  remain an RC0 decision, not an invented current MCP capability.
- Keep MCP generation/preview tools interoperable without Fruitcake coordination.
  Use standard capability discovery and optional, documented host enhancements;
  unsupported hosts/providers must report unavailable coordination, not pretend
  exclusivity. No model-authored tool arguments may grant unload authority.
- Lifecycle authority is deployment/admin controlled and separate from an image
  account's library permissions. Preserve per-user bearer isolation and never
  retry failed private access as Shared. Avoid disclosing other users' job details.

### RC2: Admission And Turn Continuation

- Acquire request-scoped inference leases at the actual provider-call boundary,
  including streaming exhaustion/error/close. Do not hold an inference lease over
  tool execution, approval waits or user-input waits.
- Queue incompatible heavy work fairly and cancelably. Once an image handoff is
  admitted, stop admitting competing inference so background jobs cannot starve
  it or immediately reload a just-released model. Serialize multiple image calls
  sharing the pool, including calls emitted in one parallel tool batch.
- Audit all Fruitcake-controlled entry points using the pool, including task
  workers/reviewers/helpers. Coding/Pi participation is conditional on an actual
  supported integration; an external process does not obey a Python-only lock.
- Start with a single backend coordination authority. Do not allow a second worker
  to quietly create a competing in-memory lock. Either share an authoritative
  mechanism or reject coordinated mode in unsupported multi-worker deployments.
- External Ollama clients cannot be stopped from reloading models by Fruitcake's
  own queue. Document cooperative-only coverage, surface unexpected residency and
  require dedicated endpoints or participation for stronger guarantees.
- Retain generation results before cleanup/continuation. Cleanup failure must not
  replay generation, lose a completed image, or silently reload Qwen into known
  memory pressure. Surface an attention-required continuation with a safe retry
  of release/resume, not a duplicate image operation.
- Separate queue wait, render and inference deadlines. Preserve existing user
  approval/input waiting semantics. Longer queue waits must not consume the image
  render timeout or silently disable genuine execution deadlines.

### RC3: Recovery, Visibility And Soak

- Release queue entries/leases on cancellation, provider errors, disconnects and
  shutdown, with ownership-checked cleanup. Canceling one chat must not stop a
  different user's render or unload a model actively used by another request.
- Expired coordinator ownership is not proof the provider stopped. After a crash
  or stale lease, reconcile actual provider work/residency before new admission;
  do not auto-replay image generation. Unknown provider state fails closed for
  coordinated admission and offers an operator recovery path.
- Reuse existing activity/status surfaces for waiting, releasing language model,
  generating image and resuming. Do not redesign navigation, cards or inspector.
- Record bounded timing/residency/queue/release metrics and outcome codes, without
  prompts, image bytes, filesystem contents or credentials. Observe memory pressure
  and swap IO, not just total allocated swap, which need not immediately fall.
- Feature-flag rollout; disabled mode preserves current behavior. Rollback drains
  existing ownership safely, not by bypassing an active lease. Keep-alive remains
  configurable; do not globally force zero retention as the final architecture.

## Acceptance And Exit

- A real Qwen -> ImageLab -> Qwen turn completes with the correct image/tool result
  and original conversation, even though the LLM was unloaded in between.
- Provider observations show no competing heavy model residency during the
  coordinated image interval, within the explicitly declared cooperation scope.
- A concurrent chat, scheduled job and second image request queue and resume
  without deadlock, starvation, cross-account leakage or duplicate generation.
- Active inference is never force-unloaded; image release is confirmed before
  LLM continuation. Failure/cancel/restart/revocation paths preserve these rules.
- Tests cover stream lifecycle, provider/queue races, partial failures, lease-owner
  changes, bounded timeouts, unsupported providers and configuration rollback.
- Compare total end-to-end time, including reload/prompt reevaluation, and memory
  pressure against a safe baseline. Set measured targets after RC0; do not promise
  zero swap, instantaneous reclamation, or a speedup on every workflow.
- Complete an operator-approved live walkthrough and short soak, then close this
  branch. Synthetic tests alone do not prove ComfyUI/Ollama memory was reclaimed.

## Deferred

Distributed workers, remote placement, adaptive byte-budget scheduling, automatic
model/quality downgrades, cloud fallback, arbitrary app eviction, universal control
of external clients and a general agent-orchestration platform are out of scope.
This sprint changes residency/admission, not permissions, persona behavior or
image-library/export semantics.
