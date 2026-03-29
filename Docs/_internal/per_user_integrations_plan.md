# Phase Proposal — Per-User Integrations And OAuth Onboarding

## Status

- Proposed future phase
- Not scheduled into active execution
- Depends on the existing encrypted `Secret` infrastructure already in `main`

## Why This Phase Exists

Fruitcake currently treats several third-party integrations as deployment-wide credentials. That is acceptable for a single-user operator setup, but it breaks down for the multi-user product direction:

- one household member cannot connect their own calendar account without replacing the shared credential
- a second user cannot safely isolate their Google or Apple data from the first
- future Gmail and contacts integrations will inherit the same flaw unless the credential model is corrected first

This phase introduces a per-user integration layer that preserves the local-first architecture while making authenticated providers actually multi-user-safe.

## Product Goal

Each authenticated user can connect their own supported external account and Fruitcake will use that account only for that user’s requests.

Initial goal:
- Google Calendar per-user
- Apple CalDAV per-user

Explicitly not in the first slice:
- Gmail
- Google Contacts
- merged/shared family calendar view
- admin “connect on behalf of user”

## Phase Guardrails

This phase should only be started when all of the following are true:

1. Multi-user real usage is blocked by shared credential behavior, not just theoretically limited by it.
2. Current calendar write/read reliability remains stable under soak.
3. The team is willing to make an explicit product decision about fallback behavior for users who do not connect an account.

If those are not true, this should remain planned work, not active work.

## Non-Negotiable Decisions

### 1. Secret references must be durable

`UserIntegration` must reference stored secrets by stable database identity, not by secret name convention.

Reason:
- names are mutable conventions
- IDs are actual references
- per-user secret naming is too easy to collide or drift

### 2. OAuth ownership must be explicit

Recommended split:
- iOS owns PKCE `code_verifier` / `code_challenge`
- backend owns signed `state`
- backend exchanges code for tokens and stores them

Do not split PKCE responsibility across client and server ambiguously.

### 3. Fallback behavior must be a product policy

There are two viable modes:

- `strict_per_user`
  - no connected account means no personal authenticated provider access
- `hybrid_household`
  - no connected account falls back to a deployment-wide shared provider

Recommendation for the first implementation:
- `strict_per_user` for authenticated per-user calendar access
- any shared household calendar should be represented intentionally later, not as a quiet fallback

### 4. One active integration per user/provider/service unless explicitly expanded later

The schema should enforce the actual product rule.

Recommended v1 rule:
- one active integration per `(user_id, provider, service)`

If multiple accounts per service are ever desired, that should be a later explicit expansion, not an accidental outcome of a loose schema.

### 5. Token refresh must be concurrency-safe

Resolver-driven token refresh is the right shape, but the implementation must assume concurrent requests.

Minimum expectation:
- only one refresh should win and persist for a given integration at a time
- parallel reads should not corrupt token state

## Proposed Architecture

### New `UserIntegration` model

Migration target:
- `app/db/migrations/versions/030_user_integrations.py`

Suggested model shape:

```python
class UserIntegration(Base):
    __tablename__ = "user_integrations"

    id: int
    user_id: int                  # FK users.id, cascade delete
    provider: str                 # "google" | "apple"
    service: str                  # "calendar" | future: "gmail" | "contacts"
    status: str                   # "connected" | "disconnected" | "expired" | "error"
    account_email: str | None     # display and operator diagnostics
    scopes: JSON | None           # Google only

    access_token_secret_id: int | None
    refresh_token_secret_id: int | None
    credential_secret_id: int | None

    expires_at: datetime | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
```

Expected DB rules:
- unique active integration per `(user_id, provider, service)`
- indexed lookup on `(user_id, service, status)`

### New integrations package

Add:
- `app/integrations/google_oauth.py`
- `app/integrations/resolver.py`

Responsibilities:

`google_oauth.py`
- build Google auth URL inputs
- exchange code for tokens
- refresh access tokens
- revoke tokens

`resolver.py`
- resolve active provider credentials for a user and service
- retrieve secrets via existing secret infrastructure
- perform bounded token refresh for Google
- return normalized provider credentials to MCP/tool callers

### New `/integrations` API

Add:
- `app/api/integrations.py`

Endpoints:

```text
GET  /integrations
GET  /integrations/google/auth-url
POST /integrations/google/callback
POST /integrations/google/disconnect
POST /integrations/apple/connect
POST /integrations/apple/disconnect
POST /integrations/{id}/refresh
```

Rules:
- no plaintext tokens ever returned to the client
- callback flow must validate signed `state`
- disconnect must revoke/delete credentials and mark the integration inactive

### Calendar integration resolution

Primary backend consumer:
- `app/mcp/servers/calendar.py`

Change direction:
- resolve provider credentials per authenticated user when `user_context.user_id` is present
- use deployment-wide/global provider settings only where that behavior is explicitly intended

Recommended v1 behavior:
- chat/user-driven calendar calls use per-user integration lookup
- background/admin/global fallback remains a separate deliberate path, not a silent default

## iOS Scope

Add:
- `FruitcakeAi/Views/Settings/IntegrationsView.swift`
- `FruitcakeAi/Services/IntegrationService.swift`

User-facing scope:
- list connected integrations
- connect Google Calendar with `ASWebAuthenticationSession`
- connect Apple Calendar via Apple ID + app-specific password
- disconnect accounts

Important constraint:
- no client-side token persistence
- iOS should broker auth flow and submit code/verifier to backend only

## Scope Boundaries

### In Scope For Phase A

- `UserIntegration` model and migration
- Google Calendar per-user OAuth
- Apple CalDAV per-user credential onboarding
- `/integrations` API
- Calendar credential resolution by user
- iOS connected-accounts UI

### Explicitly Out Of Scope

- Gmail
- Contacts
- shared family aggregation layer
- admin-managed user integrations
- Android parity work
- broad provider abstraction beyond what calendar requires

## Verification Goals

1. User A and user B can connect different Google Calendar accounts and see only their own events.
2. Apple CalDAV works per-user with app-specific password flow.
3. Expired Google access tokens refresh transparently without cross-user leakage.
4. Disconnect removes usable credentials and future requests fail cleanly.
5. Users without a connected account follow the chosen product policy explicitly.

## Risks

1. Quiet fallback to shared credentials would undermine the whole isolation goal.
2. Weak `state` validation would turn the OAuth flow into a cross-user or replay risk.
3. Loose uniqueness rules would make reconnect/disconnect semantics ambiguous.
4. Resolver-side token refresh without concurrency discipline could create hard-to-debug intermittent failures.

## Recommendation

This is a good future phase, but only as a bounded Phase A:

- calendar only
- one active integration per user/provider/service
- explicit per-user isolation
- no Gmail yet

That makes it substantial enough to solve the real architectural problem without turning into a broad “accounts platform” rewrite.
