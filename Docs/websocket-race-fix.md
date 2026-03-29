# WebSocket Duplicate Send Fix — Handoff Notes

## Root Cause

`WebSocketManager` was not actor-isolated. Two execution contexts were mutating its shared properties (`responseContinuation`, `isConnected`, `webSocketTask`) concurrently without synchronization:

- **MainActor** — `connect()`, `disconnect()`, `sendAndReceive()`
- **Background Task executor** — `receiveLoop()` and `handleIncoming()`

The specific race: when `connect()` is called to start a new session, a new `receiveLoop` is spawned immediately. But the **old** `receiveLoop` is still alive on the background executor, waiting for its cancelled `task.receive()` to throw. When it does, its error handler calls `responseContinuation?.finish()` and sets `isConnected = false` — wiping the **new** session's continuation and marking the new connection as dead. This caused the stream in `sendMessage` to end prematurely, which triggered the `defer` block, which cleared `sendClaimed` — opening the door for a second send attempt.

A secondary issue: the WS frame was sent via a fire-and-forget `Task { try await task.send(...) }` with no error handling. Send failures were silently discarded.

---

## Changes Made

### `WebSocketManager.swift`

**1. Added `@MainActor` to the class**

```swift
// Before
@Observable
final class WebSocketManager: NSObject {

// After
@MainActor
@Observable
final class WebSocketManager: NSObject {
```

All property access now happens on the MainActor. The receive loop's Task body hops to MainActor on resume after each `await task.receive()`. No blocking — I/O is still async on background threads.

**2. `sendAndReceive` changed from `throws -> AsyncStream` to `async -> AsyncStream`**

The fire-and-forget send Task is replaced with a direct `await`. Send errors are surfaced as `.error` events into the stream rather than dropped silently. JSON encoding errors are also caught and streamed.

```swift
// Before
func sendAndReceive(...) throws -> AsyncStream<WSEvent> {
    ...
    Task { try await task.send(.string(text)) }   // silent failure
    return stream
}

// After
func sendAndReceive(...) async -> AsyncStream<WSEvent> {
    ...
    do {
        try await task.send(.string(text))
    } catch {
        continuation.yield(.error("WebSocket send failed: \(error.localizedDescription)"))
        continuation.finish()
        responseContinuation = nil
    }
    return stream
}
```

---

### `ChatView.swift`

**3. Added `@State private var activeSendTask: Task<Void, Never>?`**

Stores the Task handle returned by `sendIfReady` so it can be cancelled on session switch.

**4. `sendIfReady` stores the Task handle**

```swift
// Before
Task { await sendMessage(text, sessionId: sessionId, clientSendID: clientSendID) }

// After
activeSendTask = Task { await sendMessage(text, sessionId: sessionId, clientSendID: clientSendID) }
```

**5. `switchSession` cancels in-flight send before disconnecting**

```swift
@MainActor
private func switchSession(sessionId: Int) async {
    activeSendTask?.cancel()   // ← new
    activeSendTask = nil       // ← new
    wsManager.disconnect()
    ...
}
```

The `sendMessage` defer block handles cleanup of `isSending`/`sendClaimed` on cancellation.

**6. `sendMessage` defer block also nils `activeSendTask`**

```swift
defer {
    isSending = false
    sendClaimed = false
    showToolIndicator = false
    activeClientSendID = nil
    activeSendTask = nil   // ← new
    streamingContent = ""
}
```

**7. Event loop binds to `connectionID` and checks cancellation**

```swift
let expectedConnectionID = wsManager.connectionID
let responseStream = await wsManager.sendAndReceive(...)   // await, not try

eventLoop: for await event in responseStream {
    guard !Task.isCancelled else { break eventLoop }                          // ← new
    guard wsManager.connectionID == expectedConnectionID else { break eventLoop }  // ← new
    switch event { ... }
}
```

Stale events from a raced old loop are discarded before they can be written to the new session's messages. Logged as `ws_stale_event_discarded` if triggered.

---

## What Was Not Changed

- `sendIfReady` guard logic (`canSend`, `sendClaimed`, `isSending`) — correct, untouched
- `recentSendBySession` fingerprint dedup — still in place as secondary net
- `defer` block structure in `sendMessage` — unchanged except `activeSendTask = nil` added
- `disconnect()` internals — unchanged
- REST path, `APIClient` — unchanged
- backend stale-payload replay bug was fixed later in `app/api/chat.py`; this note only covers the Swift/WebSocketManager race fix

---

## Verification Checklist

1. Normal WS send → `.done` arrives → assistant message appears, state clears
2. Session switch mid-send → Task cancels cleanly, no stuck UI, no duplicate on new session
3. Network drop mid-stream → stream exhausts without terminal event, `defer` cleans up
4. Rapid double-tap send → `sendClaimed` still blocks second send
5. Build with `-strict-concurrency=complete` → no actor isolation warnings in WebSocketManager
6. Backend logs → `client_send_id` appears once per send
