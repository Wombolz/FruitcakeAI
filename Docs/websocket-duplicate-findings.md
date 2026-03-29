# WebSocket Duplicate Findings

## Confirmed Findings

Date:
- 2026-03-29

Scope:
- Chat websocket duplicate-send investigation
- Session `1342` provided the first decisive backend evidence

## What The Evidence Proved

1. The visible Swift send path was not duplicating the request in the observed repro.
- One `send_if_ready_enter`
- One `send_message_enter`
- One `ws_send`
- Normal streamed completion with `type=done`

2. The backend only received one websocket payload for the turn.
- `chat.websocket_payload_received ... websocket_message_index=1`
- No `websocket_message_index=2` for the same socket / send id

3. The duplicate rejection happened after successful completion without a second received payload.
- `chat.websocket_message_done`
- then `chat.duplicate_client_send_id_rejected`
- same `client_send_id`
- same `websocket_id`

## Root Cause Hypothesis

Most likely bug is stale websocket message state being reused in `app/api/chat.py` after a completed websocket turn.

Current websocket loop behavior:
- first message is read into outer-scope variables:
  - `data`
  - `user_message`
  - `client_send_id`
  - `allowed_tools`
  - `blocked_tools`
- `_start_message_run(...)` uses those variables
- when `active_message_task` finishes and `receive_task` was not done, the handler cancels `receive_task` and continues
- but it appears to leave the previous message variables populated
- on the next loop iteration, `active_message_task is None`, so the loop can reuse the stale first message
- that second `_start_message_run(...)` then hits the duplicate `client_send_id` guard

This hypothesis matches the observed pattern:
- one real payload received
- normal completion
- duplicate guard fires afterward
- no second payload index logged

## Client-Side Findings

The client-side work improved containment but did not identify the original duplicate source:
- post-terminal websocket error frames are now ignored
- websocket state is now modeled as:
  - `disconnected`
  - `connecting`
  - `connected`
- same-session `connecting` sockets no longer force a reconnect in `ensureConnected(...)`
- traces are strong enough now to distinguish:
  - UI send path
  - websocket lifecycle
  - post-terminal frames

## Next Fix

Backend websocket loop in `app/api/chat.py` should be refactored so a new run can only start from a freshly received payload, not from outer-scope message variables that can survive across loop iterations.

Minimum safe fix:
- after a completed turn with no `receive_task` payload, clear:
  - `data`
  - `user_message`
  - `client_send_id`
  - `allowed_tools`
  - `blocked_tools`

Better fix:
- restructure the loop so message payload state is local to each received frame and cannot be replayed by control flow alone.
