# Ordered Coding Transcript And Approvals

## Changes

The Mac coding transcript now renders one ordered timeline per run instead of separate assistant-text, action, and approval buckets. Each visible item keeps the sequence of its first persisted event. Streaming text and its finalized snapshot update the same message block; a later assistant turn gets a new block after the actions it follows. Operation output updates its existing action row rather than appending or moving it. Empty reset plus text fragments still reconstructs one complete message.

Approval requests appear at their request event position. Pending requests remain actionable, with a visible count. Resolved requests collapse into expandable history at the same position and are retained when the run finishes. There is no three-approval display limit. Completed history can be reconstructed with **Load Operation History**; the initial unloaded history still shows the stored final response as a fallback.

New approval stream events include `expires_at` and the complete public preview so the client can render each request immediately without a separate HTTP call. Reconnect seeds records from the API and replays the ordered stream. Snapshot responses merge by durable ID rather than replacing the list; an old pending snapshot cannot remove a newer request or revert a resolved decision. Old backend events without expiry retain the HTTP fallback. Session changes still reject late responses.

## Lifecycle Correction

Previously, completing any one operation cancelled all pending approvals in that run. Parallel tool calls could therefore invalidate unrelated approvals. The backend now stores the tool-call association privately with the approval and settles only that operation's outstanding request. Other pending requests remain pending; Stop/run completion/recovery still settles the whole run. Expiration and cancellation produce explicit `approval.resolved` events with a `status`.

The approvals API orders records by the persisted `approval.requested` event sequence, not UUID or timestamp. Responses add nullable `request_sequence`, derived from existing events; no database migration or event schema bump is required. Raw runtime confirmation IDs and private association metadata remain excluded from public responses/events.

## Test And Update

Restart the backend using the normal `./start.sh` and rebuild the native `codex/coding-runtime-client` branch. There is no new migration or setting, and the existing titlebar, columns, inspector/diff resizing, and ordinary Chat view are untouched.

Test a run with more than three approvals: assistant comments should remain before/after their corresponding actions, approving one request must not cancel the others, and the remaining requests must stay visible in request order. Refresh/reconnect and load completed operation history to check the same ordering. Approving a file change still requires exact diff review and hash acknowledgement; this slice does not loosen write authority.

Verification: 203 isolated backend coding tests pass, including installed Pi fixtures and seven-request lifecycle/order checks. Forty-one focused Swift tests pass, including seven streamed approvals without per-request HTTP fetching, stale snapshot reconciliation, message/action ordering, fragmented snapshots, and an offscreen render of the actual seven-approval transcript. Mac app/test bundles and the iOS Simulator application compile with signing disabled. Offscreen rendering is not a substitute for signed-in live window validation.
