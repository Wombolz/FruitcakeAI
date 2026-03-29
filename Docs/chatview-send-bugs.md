# ChatView — Duplicate Send & Reliability Issues

**File:** `FruitcakeAi/Views/Chat/ChatView.swift`
**Reviewed:** 2026-03-29

---

## Issue 1 (Critical) — WebSocket stream exhausts without state cleanup

### What's wrong
The `for await event in responseStream` loop in `sendMessage` only resets `isSending`, `sendClaimed`, `showToolIndicator`, and `activeClientSendID` inside the terminal `case` branches (`.done`, `.error`, `.personaSwitched`). If the `AsyncStream` ends naturally — e.g. the WebSocket drops mid-response without sending a terminal frame — the loop exits and **none of these flags are ever reset**.

Result: the UI freezes in a permanent "sending" state until the app is relaunched. On relaunch all `@State` is reset, the fingerprint guard is gone, and the message can be sent again — the most likely path to the observed repeated-send behaviour.

### Fix — replace all manual resets with a single `defer`

Add a `defer` block at the top of `sendMessage` and remove every redundant manual reset at each early return and inside each `case`:

```swift
private func sendMessage(_ text: String, sessionId: Int, clientSendID: String) async {
    defer {
        isSending = false
        sendClaimed = false
        showToolIndicator = false
        activeClientSendID = nil
        if !streamingContent.isEmpty { streamingContent = "" }
    }

    isSending = true
    showToolIndicator = true
    streamingContent = ""
    loadingError = nil
    // ... rest of function unchanged, remove all manual resets below
}
```

This also covers every existing early-return path (offline, WS not connected, `sendAndReceive` throws), removing five sets of duplicated cleanup lines.

---

## Issue 2 (Critical) — `clientSendID` omitted from REST fallback body

### What's wrong
The WebSocket path passes `clientSendID` to `wsManager.sendAndReceive`, allowing the backend to deduplicate by that key. The REST fallback in `sendViaREST` uses a `SendBody` struct that does **not** include `clientSendID`:

```swift
struct SendBody: Encodable {
    let content: String
    let allowedTools: [String]?
    let blockedTools: [String]?
    // clientSendID is missing
}
```

Any message sent via the REST path has no idempotency key, so the backend cannot detect or reject a duplicate request.

### Fix

```swift
struct SendBody: Encodable {
    let content: String
    let clientSendId: String
    let allowedTools: [String]?
    let blockedTools: [String]?
}

// pass it at the call site:
body: SendBody(
    content: text,
    clientSendId: clientSendID,
    allowedTools: overrides.allowedTools.isEmpty ? nil : overrides.allowedTools,
    blockedTools: overrides.blockedTools.isEmpty ? nil : overrides.blockedTools
)
```

Ensure the backend `/chat/sessions/{id}/messages` endpoint accepts and deduplicates on `clientSendId` the same way the WebSocket handler does.

---

## Issue 3 (Medium) — `sendIfReady` duplicates the `canSend` guard

### What's wrong
`canSend` and the `guard` in `sendIfReady` check exactly the same three conditions:

```swift
private var canSend: Bool {
    !inputText.trimmingCharacters(in: .whitespaces).isEmpty && !isSending && !sendClaimed
}

private func sendIfReady(sessionId: Int) {
    let text = inputText.trimmingCharacters(in: .whitespaces)
    guard !text.isEmpty, !isSending, !sendClaimed else { return }   // ← duplicate
    ...
}
```

If a condition is ever added to `canSend` (e.g. connectivity check), the guard must be updated separately or the protection diverges silently.

### Fix

```swift
private func sendIfReady(sessionId: Int) {
    guard canSend else { return }
    let text = inputText.trimmingCharacters(in: .whitespaces)
    ...
}
```

---

## Issue 4 (Medium) — Silent fingerprint dedup drop, no user feedback

### What's wrong
When the same normalised message is sent to the same session within 120 seconds, `sendIfReady` silently returns after only a `print` statement. The user sees nothing — no error, no toast, no shake. They will assume the send failed and retry, potentially causing the backend to receive the same message anyway via a different session or after the 120 s window expires.

```swift
// line ~566
print("[ChatTrace] send_blocked_duplicate ...")
return   // ← no UI feedback
```

### Fix

Set a transient user-visible error (or a dedicated `isDuplicateBlocked` flag) so the send button or input bar communicates the block:

```swift
loadingError = "Message already sent — please wait before resending."
Task {
    try? await Task.sleep(for: .seconds(3))
    loadingError = nil
}
return
```

---

## Issue 5 (Low) — Async state-mutating methods lack `@MainActor`

### What's wrong
`sendMessage`, `sendViaREST`, `sendViaOnDevice`, and `switchSession` all mutate `@State` properties but are not annotated `@MainActor`. They are called from unstructured `Task { }` closures inside a SwiftUI view, which is implicitly `@MainActor` at the call site, but the async functions themselves carry no actor constraint. Under Swift 6 strict concurrency this produces warnings and may surface data races.

### Fix

Annotate all async methods that touch view state:

```swift
@MainActor
private func sendMessage(_ text: String, sessionId: Int, clientSendID: String) async { ... }

@MainActor
private func sendViaREST(_ text: String, sessionId: Int, clientSendID: String, overrides: SessionToolOverrides) async { ... }

@MainActor
private func sendViaOnDevice(_ text: String) async { ... }

@MainActor
private func switchSession(sessionId: Int) async { ... }
```

---

## Checklist

- [ ] Add `defer` block to `sendMessage`; remove all duplicate manual resets
- [ ] Add `clientSendId` to `SendBody` in `sendViaREST` and wire backend dedup
- [ ] Replace duplicated guard in `sendIfReady` with `guard canSend else { return }`
- [ ] Show user-visible feedback when fingerprint dedup blocks a send
- [ ] Annotate async state-mutating methods with `@MainActor`
