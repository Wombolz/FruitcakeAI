# ImageLab Account Tokens

This interim backend-only configuration assigns ImageLab credentials to
Fruitcake **users**, not personas. No database migration or client update is needed.

## Setup

1. In ImageLab's Administration > MCP Clients, issue a token for the desired
   managed account. Keep the token private; it is shown only once.
2. Find the numeric Fruitcake user ID in the authenticated `/auth/me` response
   or the administrator's `/admin/users` response. Do not use the ImageLab
   account ID, a persona name, or a username.
3. Add one single-quoted JSON object to the backend's private `.env`:

   ```dotenv
   IMAGELAB_USER_TOKENS_JSON='{"1":"token-for-user-1","7":"token-for-user-7"}'
   ```

   Values are the raw token only, without the `Bearer ` prefix. The default is
   `'{}'`, meaning everyone uses tokenless Shared access. Keep `.env` out of Git,
   screenshots, logs and chat. Apply normal private-file permissions.
4. Keep the `comfyui_image_lab` server enabled in `config/mcp_config.yaml`, using
   its configured HTTP URL and transport (`streamable_http` preferred; legacy
   `http` also works). Do not add the per-user tokens to YAML headers.
5. Restart the backend from the checkout containing this feature using the
   normal startup flow. Tokens are read at startup; edits/rotation require a
   restart. An already-open dashboard may need closing and reopening.

## Behavior And Boundaries

- A mapped user gets a separate connection, tool catalog and MCP App resource
  links. Model tool discovery, tool execution, dashboard reads and approved app
  actions all select the account from authenticated server-side user context.
- An unmapped user sends **no Authorization header** and uses ImageLab Shared.
  Fruitcake deliberately ignores ImageLab-wide Authorization entries in both
  `headers` and `headers_from_env`, including mixed-case spellings. Other headers
  and other MCP servers are unaffected.
- A rejected, revoked or unreachable configured account never retries as Shared.
  Failed initial discovery leaves that user's ImageLab tools unavailable; correct
  the configuration/access and restart to rediscover them. Previously discovered
  tools report an error if access is revoked later. Requests are not replayed.
- Malformed mappings fail configuration validation rather than silently removing
  authentication. Positive canonical numeric IDs and nonempty bearer-token
  strings are required; duplicate IDs and blank entries are errors.
- Bearer credentials stay in backend connection headers, not model arguments,
  app HTML, approval payloads or browser storage. Reflected plaintext credentials
  in tool catalogs/results/errors are redacted. Transport connections and caches
  are never reused across users or with Shared.
- `/admin/mcp/diagnostics` reports per-user ImageLab connection status without
  including credentials. Its main server/tool list continues to describe Shared.
- Assign a distinct ImageLab token to each Fruitcake user. Deliberately assigning
  tokens for the same remote account means those users share that remote library.
  Shared remains shared, not a private fallback. Persona switches retain the
  signed-in user's account; no persona-specific override is implemented here.
- ImageLab remains responsible for token validity, account grants and supported
  account types. Fruitcake does not log in to ImageLab or acquire/refresh tokens.
  This configuration does not implement the separate persona-image upload handoff.

## Acceptance Checks

Use a harmless library/status read with two differently mapped users and an
unmapped user. Verify their ImageLab account/library differs appropriately and
the unmapped user sees Shared. Open and interact with each dashboard as well.
Revoke one mapped user's token in ImageLab: that user's next request must fail,
while the other user and Shared continue working. Automated isolation, redaction
and failure-path tests are in `tests/test_imagelab_accounts.py`.

Private generated-image cards use a separate read-only resource contract. See
[Image Preview Coordination](imagelab_image_previews.md) for the paired ImageLab
implementation request; displaying a private image must not implicitly export it.
