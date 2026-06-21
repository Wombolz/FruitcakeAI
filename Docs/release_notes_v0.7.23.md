# Release Notes v0.7.23

## Summary

This release closes the remaining backend gap in chat task-draft resolution by making live assistant messages immediately identifiable and by persisting accepted or denied draft state against the original assistant response.

## Included Changes

- chat send responses now include the persisted assistant `message_id` in both REST and websocket paths
- assistant task-draft metadata now persists on chat messages and session history, including draft status and linked task ids
- task drafts can now be explicitly accepted or denied through chat-message-scoped endpoints instead of relying on duplicate-prone client reconstruction
- legacy draft status values such as `created` now normalize to user-facing `accepted` history state without requiring a migration

## Notes

- focused task-draft backend tests passed before release, including live `message_id`, accept, deny, existing-task linking, legacy normalization, and websocket `done` payload coverage
- the corresponding SwiftUI client work is being wrapped separately, but the backend release now provides the canonical identity and resolution contract the client needs
