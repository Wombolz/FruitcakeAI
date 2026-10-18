# Approved Offline Container Commands

## Scope

Coding sessions can explicitly enable `fruitcake_run` to propose commands/tests.
Fruitcake executes each approved invocation in a new Linux Docker container, never
through host Bash or Pi's built-in bash tool. This is separate from the existing
shell MCP container: its configuration, workspace mount, and disabled networking
are unchanged. No messaging, localhost bridge, downloads, publishing, or
unrestricted host execution was added to this slice.

**Commands can modify or delete any file in their managed checkout.** They are
not restricted to individually approved file edits. Enabling command mode
acknowledges that broader authority; each invocation still requires approval.
Use **Changes** afterward to inspect the actual worktree. Command records are not
per-file attribution, rollback checkpoints, or proof that a test was sufficient.

## Operator Setup And Test

1. Keep Docker running. The existing `fruitcake/mcp-shell` image is the default.

   The reviewed stdlib-only image intentionally contains no pip, setuptools,
   wheel or ensurepip. Python/unittest and GNU timeout remain available. Package
   installation is not supported inside these offline command containers; use a
   separately reviewed prebuilt command image if a project needs dependencies.
   See [container maintenance](container_maintenance.md).
   Its Python-based Linux environment is suitable for a first smoke test, not a
   promise that Node, project packages, macOS frameworks, or host tools exist.
   `CODING_COMMAND_IMAGE` selects an operator-installed image; empty disables the
   feature. Custom images need `/bin/sh` and GNU `/usr/bin/timeout`. Images are
   inspected locally and pinned by image ID before approval, never automatically
   pulled. Build/provision dependencies explicitly outside this tool.
2. Restart the backend with the normal `./start.sh` from `scripts/`, applying
   migrations through **055**, and rebuild the native coding client branch.
   Existing sessions remain command-disabled after migration. Implementation tests
   do not restart the live backend or migrate its database.
3. Open an idle session with a ready managed checkout. Choose **Enable Container
   Commands**, review the writable-checkout/offline warning, and confirm. This is
   separate from **Enable Approved Edits**; either capability can be enabled alone.
4. Prompt: `Use fruitcake_run to propose python --version in the checkout root.
   Wait for my approval. Do not use host Bash.` Review the exact command,
   directory, timeout, image ID, and network policy, then choose **Approve Command**.
5. Observe ordered operation/output, exit status, and saved results in the
   approval's expandable history. **Load Operation History** reconstructs older
   operations/approvals. Try Deny and Stop as well; denied invocations do not run.
6. Disable commands while idle to remove tool exposure. A lost mode response
   blocks submission until Refresh reconciles state. Reconcile lost approval
   responses rather than automatically resending a command.

## Execution Boundary

- Only the validated managed checkout is mounted, at `/workspace`, writable. The source repository, personal directories, and Docker socket are not mounted.
- The `.git` pointer is over-mounted read-only. Its source Git common directory is not mounted, so in-container Git is not a supported workflow in this slice. Host-owned Changes/diff inspection remains available.
- Network is `none`: no internet, LAN, Mac localhost services, or model endpoint from the command container. Pi's host-side model transport is unaffected.
- Root filesystem is read-only, capabilities are dropped, new privileges are disabled, and execution uses the backend operator's numeric UID/GID rather than container root. `/tmp` is disposable 256 MiB tmpfs; no host credential environment is forwarded.
- Each container has 512 MiB memory, one CPU, and a 128-process limit; at most two execute concurrently. No host PID namespace, devices, port publication, or privileged mode is requested.
- Commands default to 120 seconds, maximum 300. Container-side GNU timeout enforces a deadline independently of the backend, with two seconds for forced termination. A host fallback deadline and force-removal handle completion, timeout, and Stop. Exit codes 124/137 are timeout conventions unless Docker reports OOM.
- Output is continuously drained, retained up to 64 KiB per stdout/stderr stream, and explicitly marked when capped. Pi receives at most 16 KiB per stream with truncation metadata. Transcript previews have a separate display budget. Full file-backed build logs are deferred, not claimed as available.

Docker adds real isolation, not perfect containment. Trust in the Docker
installation/image remains necessary. Mounted checkout contents can themselves
contain secrets and are writable. Container/daemon vulnerabilities remain residual
risks. This is not a universal hostile-code sandbox or a native macOS build environment.

## Approval And Lifecycle

The command-enabled extension explicitly tells the model to **call `fruitcake_run`
to request approval**, not to stop after printing a shell command or asking for
approval in chat. The tool call creates the review card; calling it alone cannot
execute anything. Use `working_directory:"."` (or omit it) for the checkout root,
not `/workspace` or the absolute Mac checkout path. `/workspace` is the container
path for file references *inside the command text*. Invalid argument types,
oversized/empty commands, invalid timeouts, and unsafe/absolute working directories
are rejected as tool errors before sending a private approval proposal, allowing
the model to correct the request without failing the entire host run. The host
still independently validates every proposal. Prose, including text styled as an
approval card or a chat reply saying "approved", is never parsed into execution
or treated as approval. A model may still fail to call tools; these instructions
are guidance, not an enforcement or automatic-retry mechanism. After changing the
extension, restart the backend to retire already-loaded Pi processes; no client
rebuild, migration, or permission widening is required.

The extension proposes command text, checkout-relative directory, and timeout over
a private Pi RPC input/result exchange. It never starts a shell or Docker itself.
The host validates opt-in, user/workspace/model authority, checkout identity,
directory containment, and Stop state. Model policy recognizes `fruitcake_run`,
`bash`, and `shell_exec` as aliases for this capability.

The client acknowledges a SHA-256 hash binding the complete invocation, image,
fixed network/filesystem policy, and command ID. Legacy boolean-only or file-change
approvals cannot authorize commands. New approvals wait without expiry and bind
an invocation, not an immutable snapshot of every checkout file. The command's
execution deadline starts only after approval; see [approval waits](coding_approval_wait.md).

Approval is persisted before dispatch and returns without waiting for completion.
The host revalidates execution authority, saves queued/running/terminal state and
bounded output in approval metadata, and forwards the result to Pi. Lost delivery
does not erase saved outcomes; replay never automatically reruns a command.

Stop/finalization cancel command jobs. Startup removes only Fruitcake-labelled
coding command containers and settles unfinished records. Previously running
commands become `uncertain`, not retried. Failed cleanup verification blocks new
commands pending operator inspection/restart. Cleanup does not undo checkout
writes. Backend crashes may leave exited containers until recovery, while the
independent watchdog still bounds command runtime.

## Additive API

- Migration `055_coding_command_mode` adds `CodingSession.command_mode`, false by default.
- Status adds `container_commands`: configured runner support, not proof Docker/image provisioning is healthy.
- `POST /coding/sessions/{id}/command-mode` accepts only `{"enabled":true|false}`, requires idle state, and checks checkout/image availability when enabling.
- Approval responses/events add nullable `command`; resolve requests require its `command_hash` when approving. Denial needs no hash.
- `GET /coding/runs/{id}/commands` returns owner-scoped policy, status, and bounded saved output, not private runtime request IDs.
- Additive `command.started`/`command.recorded` events accompany existing operation/output events. No event-schema bump or replacement chat transport is needed.

## Verification And Next Gate

Final verification: 249 isolated backend coding tests and 46 focused Swift tests
pass. macOS app/test bundles and the iOS Simulator app build with signing disabled.
The native approval card was rendered offscreen and inspected; signed-in live app
behavior remains an operator smoke test, not claimed as automated certification.

Tests cover opt-in, ownership/model policy, exact-hash review, interaction fallback
refusal, Deny/expiry/Stop, duplicate resolution, recovery without replay, output
bounds, and the actual native approval card. Disposable Docker fixtures cover
mount/Git-pointer/environment/network boundaries, timeout/cancellation, output
caps, and the detached watchdog. Installed Pi uses a scripted local model,
including the complete Pi-to-Docker-to-result flow; tests call no real LLM.

After the operator smoke test, soak offline commands before expanding networking,
images/dependencies, log artifacts, or publish/export. Shell networking remains a
separate decision; messaging remains deferred in
`Docs/_internal/shared_messaging_subsystem_followup_idea.md`.
