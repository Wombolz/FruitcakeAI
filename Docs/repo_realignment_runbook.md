# Repo Realignment Runbook

This runbook covers the Phase 5.6 repository transition:

- backend/runtime repo: `fruitcake_v5` -> `FruitcakeAI`
- Swift client repo: `FruitcakeAi` -> `FruitcakeAI_Client`

## Checkpoint tags

Create and push checkpoint tags before any GitHub rename:

- backend: `pre-realign-backend-v0.6.5`
- client: `pre-realign-client-v0.2`

These tags are the rollback anchors for the rename window.

## Forward steps

1. Confirm both repos are clean and pushed.
2. Rename the GitHub repositories.
3. Update local `origin` remotes to the new repository URLs.
4. Verify `git fetch`, `git pull`, and `git push` still work.
5. Validate:
   - backend tests + startup health
   - Swift project open/build
   - README and roadmap links between repos

## Rollback

If the rename introduces problems:

1. Restore the old local remote URLs:
   - backend: `git remote set-url origin git@github.com:Wombolz/fruitcake_v5.git`
   - client: `git remote set-url origin https://github.com/Wombolz/FruitcakeAi.git`
2. Redeploy the previous backend release tag if needed:
   - `v0.6.5` is the current backend release checkpoint before Phase 5.6 repo identity changes
3. If GitHub-side rename issues block users, reopen the old repository names or add redirects there until onboarding docs are updated.

## Notes

- This sprint changes public repo identity and onboarding, not product APIs.
- The Swift repo remains a shared Apple client for iOS and macOS. Internal Xcode target renames are deferred unless a public-facing mismatch requires them.
