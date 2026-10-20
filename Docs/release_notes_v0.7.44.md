# Release Notes v0.7.44

## Summary

Chat can browse the signed-in user's Fruitcake workspace in the paired native
client's shared inspector, separate from Code's authorized repository checkout.

## Included Changes

- Authenticated, user-scoped workspace roots and lazy, version-bound directory pages.
- Complete UTF-8 text inspection up to 256 KiB and 12,000 lines, with explicit
  binary/oversized states instead of silently truncated source.
- Bounded 20 MB attachment downloads for native saves and inert image/PDF previews.
- Descriptor-relative path/link checks, root identity validation, no-store responses
  and post-wait account checks. No arbitrary host-directory or repository grants.

## Notes

Pair with native client v0.2.5 for My Workspace controls, explicit draft-only
attachments and code appearance presets. File management/editing are future work.
No migration, new dependency or required environment setting is introduced.
See [Chat: My Workspace](chat_workspace.md) for limits, rollout and verification.
