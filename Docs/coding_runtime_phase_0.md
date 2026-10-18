# Coding Runtime Phase 0

## Status

The private Pi protocol prototype and the read-only workspace experiments are implemented on `codex/coding-runtime-phase-0`. The read-only architecture gate was accepted for backend work on October 16, 2026; the [backend foundation](coding_backend_foundation.md) now implements persistence, session supervision, and HTTP/SSE. The [native read-only client](coding_client.md) passed the operator's reported manual checks; [live repository review](coding_repository_review.md) follows without enabling write or command capabilities. The findings below remain the historical Phase 0 evidence, not permission to widen the execution boundary.

The implementation is optional: importing or running ordinary Fruitcake chat does not start Pi. Pi was temporarily downloaded through npm for the experiments; it has not been added to Fruitcake's required dependencies or installed globally.

## Compatibility

- Tested package: `@earendil-works/pi-coding-agent@1.3.0`.
- Supported version: exactly `1.3.0`. Later patches and prereleases require deliberate compatibility verification before widening the gate.
- Transport: LF-delimited JSONL over the child's stdin/stdout, with stderr collected separately.
- Process platform tested: macOS, using a separate process group for each runtime.
- Persistence: `--no-session`; Fruitcake will own durable session state in the backend foundation.

Protocol references are Pi's [RPC documentation](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md), [event stream](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/json.md), and [extension interactions](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc-extension-ui.md). The installed package's source and local integration tests are authoritative for this version pin.

## Protocol And Lifecycle

The adapter distinguishes command acceptance from run settlement. `agent_end` ends a low-level agent run; automatic retries, recovery, or queued work may follow. `agent_settled` is the completion boundary. A prompt consumed by an extension can return `handled` without starting a run.

The reader handles fragmented UTF-8, CRLF, embedded Unicode line separators, malformed JSON, invalid event shapes, duplicate responses, late responses, and bounded records. A bounded event queue prevents an unattended consumer from accumulating unlimited output; overflow terminates the runtime and reports failure.

Cancellation closes the active runtime when completion cannot be established. Explicit stop clears queued steering/follow-up messages before aborting. Closing a runtime wakes pending commands and settlement waiters, closes stdin, and escalates through process-group termination when necessary. Unexpected exit is a failure regardless of its numeric exit status.

Normalized events include:

| Runtime evidence | Normalized event |
| --- | --- |
| Agent/turn start | `run.started`, `turn.started` |
| Streamed visible text | `assistant.text_delta` |
| Final visible message | `assistant.message` |
| Thinking activity | `assistant.reasoning_status` without reasoning text |
| Tool request/result | `operation.started`, `operation.completed` |
| Partial tool result | `operation.output`, marked as a snapshot |
| Extension dialog | `interaction.requested` |
| Retry | `run.retrying` |
| Turn end | `turn.completed` |
| Successful settlement | `run.completed` |
| Model error followed by settlement | `run.failed` |
| Unexpected process exit or output overflow | `session.failed` |

Unknown events retain their type for private diagnostics without forwarding arbitrary message snapshots. Reasoning is removed from both streamed updates and complete message/agent snapshots. Event text and operation payloads are bounded.

## Workspace Guard

The adapter loads Fruitcake's own `workspace_guard.mjs` explicitly while disabling automatic extension, MCP, skill, prompt-template, and project-context loading. Each process gets a temporary Pi configuration directory. Startup verifies both `get_state` and a guard acknowledgement bound to the canonical workspace and a per-process nonce.

The `tool_call` hook runs before execution and permits `read`, `grep`, `find`, and `ls`. It resolves the requested target, checks canonical containment, and replaces the tool input with the authorized absolute target. Read targets must be regular files without hard links. Directory operations inspect links and special files before delegation. Recursive checks have a conservative 50,000-entry limit and fail closed when it is exceeded. Escaping directory contents can therefore block a whole search, rather than merely omit the unsafe entry.

The guard denies shell and mutation tools; the adapter separately denies direct bash and session-changing RPC commands that bypass the normal tool path. Offline mode disables Pi's auxiliary startup/download traffic and telemetry is disabled. Model requests still use the operator-selected endpoint.

Disposable-fixture tests demonstrate:

- ordinary in-workspace reads succeed;
- an unguarded Pi process can read outside its working directory;
- guarded relative traversal, absolute outside paths, sibling-prefix paths, `@` paths, file symlinks, and directory symlinks are denied;
- recursive search with an escaping `.ignore` symlink is denied;
- Unicode-space aliases that Pi would normalize to a different filename are denied;
- hard-linked files and FIFOs are denied;
- write and bash calls cannot mutate the fixture;
- swapping a target to an escaping symlink during approval is caught by revalidation.

This guard supports an operator-controlled local alpha. It is not an OS sandbox. A compromised Pi process or trusted extension could bypass its hooks. Another process can also race filesystem changes between the final check and a built-in tool's filesystem operation. Hostile repositories or concurrent hostile filesystem writers require an enforceable sandbox or a broker using descriptor-based filesystem operations.

The conservative alias check can reject otherwise valid filenames or workspace roots containing special Unicode spaces. Ordinary ASCII spaces are supported. Unicode filename compatibility can be expanded after the tool path-normalization boundary is replaced or independently verified.

`find` additionally needs an installed `fd`/`fdfind` executable. It was absent on the development machine; Pi's offline mode prevents automatically downloading it. The first product slice should report missing search helpers as unavailable capabilities or require them during operator setup. The live Qwen experiment exercised `read`.

## Preventive Approval Evidence

An optional spike setting confirms in-workspace reads to verify the protocol boundary. The guard validates the target, waits on a timed `ctx.ui.confirm`, validates the path again after approval, and then permits execution. The adapter correlates the response with the outstanding request and rejects repeated answers.

Tests against the installed Pi process prove that:

- the next model request has not received file contents while approval is pending;
- approval allows the read;
- denial and expiry return an error without exposing the file contents;
- changing the filesystem while waiting causes revalidation to deny an escaped target.

A `tool_execution_start` event can precede this confirmation, so the client must distinguish a requested operation from one approved to execute. Product integration must map these requests into Fruitcake's durable approval records. The prototype has no durable approval store.

Write, shell, package installation, remote Git, and arbitrary network operations remain disabled. This experiment does not establish preventive approval coverage for arbitrary shell command effects.

## Model And Credential Selection

`PiModelProfile` creates a model definition in the isolated runtime directory, selects its provider/model explicitly, and checks the returned state before startup completes. Its API key is supplied through a dedicated environment variable; the generated model file contains only the variable reference. The key is excluded from process arguments and profile representations. Unrelated application and model credentials are not inherited.

The real local test used:

- endpoint: local Ollama's OpenAI-compatible `/v1` API;
- model: `qwen3.8:27b-q4_K_M`;
- fixture: a disposable Git repository containing `fixture.txt`;
- instruction: read that file through the read tool and return its token.

Pi selected the requested model, issued one successful read, returned `FRUITCAKE_CODING_READ_OK`, and emitted settled completion. Only fixture data was used.

## Measurements

Measured on the development Mac; values are samples, not production guarantees.

| Experiment | Result |
| --- | --- |
| Three direct idle Pi processes | Approximately 116-121 MiB each; 356 MiB total |
| Three direct startup-to-state times before guard work | 0.1961-0.2391 seconds |
| Three direct orderly shutdowns | 0.0060 seconds combined |
| Guarded Qwen test startup including version check | 0.3898 seconds |
| Guarded Qwen fixture turn | 28.1052 seconds |
| Guarded Qwen Pi process-tree peak RSS | 126240 KiB, approximately 123 MiB |
| Guarded Qwen orderly shutdown | 0.0101 seconds |

The Pi RSS excludes the separately running Ollama model and its model weights. Earlier npm-wrapper measurements were approximately 219 MiB per idle process; direct executable launch avoids that wrapper overhead. The scripted endpoint abort test completes within its two-second deadline.

## Reproduction

Final verification: 54 tests passed, including 19 installed-Pi integration cases against the scripted loopback endpoint. The separate real Qwen fixture exercise also succeeded. The focused suite exits normally without loading Fruitcake's database/application fixtures.

Run protocol tests independently of the full application and database fixtures:

```bash
.venv/bin/python -m pytest -q --confcutdir=tests/coding_runtime tests/coding_runtime
```

Installed-Pi tests are opt-in. They start a temporary loopback HTTP server with scripted model responses; no hosted model or credential is needed:

```bash
FRUITCAKE_TEST_PI=/absolute/path/to/pi .venv/bin/python -m pytest -q --confcutdir=tests/coding_runtime tests/coding_runtime
```

Measure an operator-installed executable:

```bash
.venv/bin/python scripts/probe_pi_runtime.py --executable /absolute/path/to/pi --instances 3
```

Exercise the local model using a disposable repository:

```bash
.venv/bin/python scripts/probe_pi_runtime.py --executable /absolute/path/to/pi --model qwen3.8:27b-q4_K_M --exercise-read --prompt-timeout 90
```

Pi must be installed deliberately by the operator. The optional probe's npm fallback is an explicit developer command; application startup does not install dependencies.

## Architecture Gate

The evidence supports proceeding to the provider-neutral backend foundation for a trusted local, read-only alpha with these decisions:

1. Use separate coding sessions and Fruitcake-owned durable run/event state; keep Pi sessions ephemeral initially.
2. Keep Pi exactly pinned at 1.3.0 until a deliberate compatibility run authorizes another version.
3. Launch an operator-installed executable directly with a selected model profile and the mandatory guard acknowledgement.
4. Start with at most two active runtime processes and evict idle processes after five minutes. These limits are now implemented in the session supervisor.
5. Permit only inspection tools and surface missing helper binaries as capability limits.
6. Keep the guard's application-level scope explicit. Enforce OS containment before supporting hostile repositories or describing the feature as sandboxed.
7. Bridge proven pre-execution dialogs into Fruitcake's existing approval infrastructure before any write-enabled product work.
8. Require a separate gate for write mode, commands, network access, and disposable-worktree mutation.

The subsequent backend foundation implements this read-only gate. Write mode remains subject to a separate decision.
