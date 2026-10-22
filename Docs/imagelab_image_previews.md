# ImageLab Read-Only Image Preview Contract

Status: Fruitcake consumer implemented for backend v0.7.45 / native v0.2.6.
ImageLab provider support was verified against the running local MCP listener on
October 22, 2026: an existing generated asset returned a validated PNG through its
configured account, while anonymous Shared access to that asset was denied. No
image generation, export or historical-message edit was performed. Clients still
need a compatible ImageLab build and a token with `profile.library.read` scope.

## Why

Authenticated `generate_image` returns `asset_id` rather than `image_path`.
Fruitcake needs an authorized preview without invoking `export_image`, copying
private library files to the workspace, reading ImageLab's database/filesystem,
or sending the bearer token to a client or model.

## ImageLab Provider Contract

Use standard MCP `resources/read`, not a new REST endpoint or model-facing tool.
Advertise the resources capability and preferably a resource template:

`imagelab://assets/{asset_id}/preview`

The asset identifier is an opaque 1-160 character ASCII alphanumeric, hyphen or
underscore value, as currently returned by generation. Do not accept paths/URLs.

Example JSON-RPC request (transport authentication remains the existing header):

```json
{"jsonrpc":"2.0","id":7,"method":"resources/read","params":{"uri":"imagelab://assets/ASSET_ID/preview"}}
```

Result:

```json
{"jsonrpc":"2.0","id":7,"result":{"contents":[{"uri":"imagelab://assets/ASSET_ID/preview","mimeType":"image/png","blob":"BASE64_IMAGE_BYTES"}]}}
```

- Exact matching URI, base64 `blob`, and `image/png`, `image/jpeg`, or `image/webp`.
- Preserve image proportions. At most 4096 pixels on either axis and 20,000,000
  decoded bytes. Smaller previews are welcome; never stretch/crop to a square.
- Check token validity, account MCP enablement, and asset ownership on **every**
  read. A URI is an identifier, not an authorization grant. Require a read/preview
  scope, not the export scope; decide explicitly how this maps to existing tokens.
- Do not fall back to Shared on invalid/revoked tokens. An anonymous connection
  must not retrieve any managed account's private assets.
- Missing/deleted/unauthorized assets should be indistinguishable resource-not-found
  errors; invalid credentials remain an authentication error.
- No export, workspace copy, filesystem paths, signed public URL, or other side
  effect. No raw image bytes or bearer credentials in logs.
- Existing `generate_image` metadata stays unchanged and backward compatible.
  Optional `preview_uri` may be added later, but Fruitcake v1 constructs only the
  fixed URI above from the returned asset ID. No arbitrary URL/URI dereferencing.

## Fruitcake Consumer

- The registry attests `source_server` separately from server/model-generated text.
  Runtime image artifacts and saved evidence retain asset IDs and dimensions.
- `GET /artifacts/chat-images/{message_id}/{image_index}` accepts no provider URL,
  credential, or arbitrary asset ID. It selects an image in an owned assistant
  message, then reads the resource using the current user's fixed MCP connection.
- Only the configured `comfyui_image_lab` provider is enabled for v1.
- The bridge validates URI, content type, base64, size, image format and dimensions;
  responds with `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`.
- Bytes go only to the authenticated native preview, never the model, chat history,
  tool audit trail, or a new persistent preview cache. Provider failure is bounded
  to 30 seconds and never triggers export, regeneration, or Shared fallback.
- Native cards preserve proportions in a bounded 460 x 420 point area. Clicking
  opens/reuses the right-side inspector tab with fit/zoom controls.
- Workspace-path images continue through the existing authenticated image API.

## Paired Acceptance Checks

1. Generate as account A: compact card displays; clicking opens inspector.
2. Read as account B or anonymously: cannot retrieve A's preview.
3. Revoke A's token or disable its MCP access: subsequent activation/refresh fails,
   with no Shared fallback. Already delivered pixels cannot be remotely recalled.
4. Delete image: refresh fails cleanly; no broken-layout or endless loading state.
5. Portrait, landscape and square: no forced blank frame or cropping; zoom scrolls.
6. Invalid base64, wrong MIME/URI, oversized image and HTML/SVG are rejected.
7. Close/reopen inspector or restart Fruitcake: stored metadata still resolves with
   fresh authorization. No export scope is needed and workspace contents do not change.

## Existing Messages

Previously saved asset-only results may have no image evidence because the old
pipeline discarded them. They cannot be safely reconstructed from assistant prose.
Do not regenerate images or edit historical chats automatically; any recovery must
use an actual original tool result with verified provider provenance.
