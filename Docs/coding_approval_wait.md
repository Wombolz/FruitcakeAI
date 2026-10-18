# Coding Approval Waits

Migration **057_coding_approval_wait** and a rebuilt native client are required.
Use the normal `scripts/start.sh` bootstrap after stopping the backend. The change
does not migrate or restart a live backend as part of implementation testing.
Older clients cannot decode a null approval deadline; update both components.

## Waiting For The User

New read confirmations, file-change approvals, and container-command approvals
have no expiry. REST and SSE expose `expires_at: null`. The native card says
**Waiting for you. No approval timeout.** Existing finite deadlines remain valid;
the migration does not silently extend pending or historical approvals.

Pi's extension dialogs also omit their timeout. Container commands are not
launched, and proposed file changes are not written, while awaiting a decision.
Stop remains available. Edit/command dialog cancellation follows the tool's abort
signal; the service's bounded Stop/cleanup path still covers stalled runtimes.

The run's `CODING_RUN_TIMEOUT_SECONDS` budget counts execution, not time with
pending human approvals. The first pending approval pauses the remaining budget;
only the last resolved/cancelled approval resumes it. The budget is not reset.
Ordinary model hangs and command execution still have deadlines. Waiting runs
retain their active-session capacity; idle eviction does not evict an active run.

## Safety And Recovery

Late approval still rechecks the user, grant, model/tool permissions, execution
root, checkout, Stop state, exact change/command hash, and current file content.
A stale file proposal cannot overwrite an external edit. A command approval binds
the invocation, not a frozen copy of every file in its writable checkout.

Denial, Stop, runtime failure, backend shutdown, and startup recovery settle
pending approvals. Restarted runs remain honestly interrupted and approvals are
cancelled, not replayed or resumed automatically. Indefinite waiting applies only
while the backend/runtime are alive, not across process loss or machine restarts.

Downgrade refuses non-expiring approval history rather than inventing expiry
timestamps. Resolve retention/schema compatibility deliberately before downgrading.

## Verification

304 isolated coding tests pass, including installed Pi 1.3.0 with a scripted
localhost model and Docker fixtures. Coverage includes waits beyond the execution
budget, approval/denial/Stop, revocation, stale-file protection, parallel approvals,
remaining-budget preservation, restart recovery, migration, and real Pi dialogs
without timeouts. All 63 focused native tests pass; macOS app/test bundles and
the iOS Simulator app build with signing disabled. The non-expiring command card
was visually inspected in an offscreen render. No live database migration,
backend restart, real model request, or operator repository mutation was performed.
