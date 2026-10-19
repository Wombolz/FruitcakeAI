# Security Baseline

This document describes the current security baseline for FruitcakeAI as of release `v0.7.39`.

It is not a formal security audit. It is the minimum operator and contributor reference for understanding:

- what the system protects by default
- what it assumes about the deployment environment
- which controls already exist
- which hardening steps are still the operator's responsibility

## Security Model

FruitcakeAI is designed as a local-first system intended to run on hardware controlled by the operator.

Security priorities in the current architecture are:

1. Keep local use fully functional without requiring cloud providers.
2. Limit cross-user access through authenticated API boundaries and persona-scoped capability controls.
3. Require explicit approval before persistent or externally mutating actions when task approval is enabled.
4. Degrade safely when local dependencies fail instead of continuing in an unknown state.
5. Treat optional integrations as additive, not load-bearing.

This baseline assumes a trusted local network or a deliberately hardened reverse-proxy/front-door. FruitcakeAI does not currently claim internet-exposed, zero-trust readiness by default.

## Current Security Controls

### Authentication and authorization

- JWT-based authentication for API access.
- Account creation, including the legacy `/auth/register` URL, requires an
  authenticated administrator. There is no public self-registration.
- Role-based authorization with current roles:
  - `admin`
  - `parent`
  - `restricted`
  - `guest`
- Admin endpoints are protected by `require_admin`.
- Inactive or missing users are rejected at the auth dependency layer.
- Administrators can assign explicit per-user model allow-lists. Those policies constrain model discovery, saved preferences, session model changes, and later turns in already-open sessions.
- Administrative authority covers identity and access metadata; it does not implicitly expose another user's chats, documents, memories, or integration credentials.

### Local-first operation

- Local Ollama is the default LLM backend.
- Cloud LLMs are opt-in via environment configuration.
- If no cloud API keys are configured, no LLM traffic leaves the machine.

### Data isolation and scoped behavior

- Per-user task ownership and chat session ownership are enforced in API routes.
- Persona-scoped blocked tools and content restrictions are applied in agent context.
- Skills are additive only and cannot bypass persona-blocked tools or task execution-profile caps.
- Memory retrieval is per-user and deliberate recall/material-use is tracked separately from passive retrieval.
- Linked-folder ingestion is disabled unless the operator configures explicit allowed roots via `LINKED_SOURCE_ALLOWED_ROOTS`.

### Task and tool safety

- Approval gating exists for persistent or externally mutating tools when task approval is enabled.
- The current default approval-gated mutation families include:
  - calendar event creation and deletion
  - persistent task creation, update, and task-plan creation
  - memory writes and memory-graph mutations
  - workspace filesystem mutations such as file writes/appends and directory creation
  - RSS source/catalog mutations such as adding/removing sources and approving/rejecting candidates
- Separately from normal mutation gating, trusted-local agent workflows may pause for explicit approval before expanding read access to a non-workspace host root. This is a distinct approval family from workspace file writes and does not widen the generic workspace filesystem sandbox.
- Market-data lookups are conditionally approval-gated only when `save_to_library=true`; read-only market lookups remain ungated.
- Scheduler guardrails pause and requeue on local LLM unavailability instead of repeatedly churning failed runs.
- Run diagnostics and admin inspection surfaces make tool use, artifacts, and skill injection observable.
- Waiting approval is exposed explicitly through:
  - task/task-step API fields `waiting_approval_tool` and `waiting_approval_reason`
  - additive structured waiting-approval payloads when the paused action carries extra context such as requested host-root path and access mode
  - admin task-run inspection payloads
  - Fruitcake MCP inspection payloads for operator/developer workflows

### Transport and secrets handling

- APNs uses token-based authentication with the Apple `.p8` key supplied by the operator.
- Sensitive settings are expected via `.env` / environment variables, not hardcoded in the repo.
- MCP integrations are explicitly configured; unused integrations are not automatically active.
- Alpha default MCP policy is conservative:
  - first-party internal MCPs may ship enabled when they are core product capability
  - Docker/third-party MCPs should remain disabled unless the operator intentionally enables them
- Skills remain admin-managed and additive only; only reviewed shared skills should be considered alpha-default.

### Persistence and auditability

- Task runs are persisted separately from tasks.
- Task run artifacts preserve prepared datasets, draft/final output, validation reports, and run diagnostics.
- Audit logs persist tool-call traces for admin review.
- Separate content-free policy events record user lifecycle and model-access changes without copying private conversation or library content into the audit trail.
- Memory deletes are soft deletes, preserving history unless explicitly redesigned later.

## Operator Responsibilities

These are mandatory for any deployment beyond personal local testing.

### Dependency maintenance status (2026-10-18)

The refreshed Python baseline and native runtime/Node rollouts are documented in
[dependencies.md](dependencies.md), including validation, private rollback
artifacts, the temporary unpatched NLTK exception and the announced October 21
Node security release. Active npm/Pi/Elgato package trees were checked against
npm advisories; this does not cover container OS packages, embedded bundle code,
unused historical installs or credentials. Database/seed credentials and JWT
secret hardening are separate operator responsibilities, not completed merely
by upgrading dependencies. The subsequent local rollout is recorded below.

### Required before shared-network use

1. Change all default seeded passwords from `config/users.yaml`.
2. Set `JWT_SECRET_KEY` to a unique, strong value; `SECRET_KEY` is not the setting name.
3. Keep PostgreSQL bound to trusted interfaces only, or place it behind host-level firewall rules.
4. Do not expose the API directly to the public internet without TLS and an authenticated reverse proxy.
5. Restrict filesystem access to `.env`, APNs keys, and any calendar credentials.
6. If linked-folder ingestion is needed, configure `LINKED_SOURCE_ALLOWED_ROOTS` narrowly and only for directories intended for Fruitcake document import.

### Strongly recommended

1. Run behind a reverse proxy that provides TLS, request size limits, and basic abuse controls. This repo ships a Caddy-based front door via `Caddyfile` and `./scripts/start_https.sh`.
2. Limit inbound access to trusted devices or a VPN if remote access is needed.
3. Rotate any compromised JWT secret, APNs key, or cloud API key immediately.
4. Review enabled MCP servers and remove any capability you are not actively using.
5. Review admin accounts and keep the number of admin users minimal.
6. Keep developer-only MCPs such as browser automation or shell execution disabled unless you are intentionally operating in a local/admin workflow.

## Public and Sensitive Endpoints

### Intentionally public

- `GET /health`
  - liveness only
  - returns status, app version, and trace id
- `POST /webhooks/trigger/{webhook_key}`
  - key-based trigger surface
  - treat webhook keys as secrets

### Authenticated but sensitive

- `/auth/*`
- `/chat/*`
- `/tasks/*`
- `/memories/*`
- `/library/*`
- `/devices/*`
- `/webhooks` config CRUD

### Admin-only

- `/admin/health`
- `/admin/users`
- `/admin/users/{id}/model-access`
- `/admin/policy-audit`
- `/admin/audit`
- `/admin/task-runs`
- `/admin/task-runs/{id}/inspect`
- `/admin/tools`
- `/admin/mcp/diagnostics`
- `/admin/skills/*`

The task-run inspect surface is the primary operator view for paused approval runs. When a run is waiting for approval, it exposes whether the run is paused, the blocked tool, and the human-readable approval reason.

## Default Exposure and Trust Assumptions

### Safe assumptions for local development

- `http://localhost:30417` backend
- local PostgreSQL
- local Ollama
- seed users present
- APNs optional

### Not safe assumptions for production/shared deployment

- default passwords
- plaintext HTTP over untrusted networks
- unrestricted local network access
- public exposure of the webhook trigger path
- enabling third-party MCP servers without reviewing their trust boundaries

## Known Limitations

These are known constraints of the current security posture.

1. No built-in TLS termination.
2. No built-in rate limiting or IP-based abuse controls.
3. Webhook trigger security is key-based rather than signed-request verification.
4. Security depends partly on local-host or LAN trust unless a reverse proxy/VPN is added.
5. MCP server trust is deployment-dependent; external MCP tools may expand the attack surface significantly.
6. The repository is local-first and privacy-focused, but not yet documented as hardened for hostile multi-tenant hosting.
7. Memory export/delete currently covers memory data only, not full account erasure across all data types.
8. Skills are reviewed guidance, not sandbox boundaries; poor skill hygiene can still expand effective prompt/tool pressure even when persona caps remain in force.
9. Linked-folder ingestion is intentionally narrowed to operator-approved roots; broad arbitrary host-folder ingestion is not part of the alpha trust model.

## Security-Sensitive Defaults and Files

- `.env`
- `config/users.yaml`
- `config/mcp_config.yaml`
- any directories listed in `LINKED_SOURCE_ALLOWED_ROOTS`
- APNs `.p8` auth key referenced by `APNS_AUTH_KEY_PATH`
- any Google Calendar service account credentials
- any Apple CalDAV app password

These should never be committed with live secrets.

## Update and Vulnerability Hygiene

Current practice in the repo:

- Python runtime upgraded to 3.11
- recent security-driven dependency updates were merged before `v0.6.8`
- skills install path now enforces preview/install invariants and converts DB uniqueness conflicts into clean `409` responses

Recommended ongoing hygiene:

1. Run `scripts/audit_dependencies.sh` against the installed environment; CI also
   runs weekly. The dated NLTK exception and baseline update procedure are in
   [Dependency Maintenance](dependencies.md).
2. Re-run `pip check` after dependency changes.
3. Review MCP server updates before enabling new images or configs.
4. Revisit this baseline whenever a new public integration surface is added.
5. Keep the set of default shared skills small and reviewed; move experimental skills out of the alpha-default path.

## Approval Model Notes

- `waiting_approval` means execution paused before the gated tool executed.
- `waiting_approval_tool` is the persisted tool identifier for compatibility and resume logic.
- `waiting_approval_reason` is the operator-facing explanation for why that tool was paused.
- Approval visibility is now intended as operability polish, not a missing runtime-safety gap: the core approval boundary is already enforced in the task runner and surfaced through task, admin, and MCP inspection paths.

## Admin Incognito Sessions

Admins can create incognito chat sessions. The honest trust boundary is:

- **Ephemeral in-app, on manual delete.** Conversation content is stored in
  the normal chat tables while the session is active and is hard-deleted
  (session and all message rows) when the admin deletes the session. This is
  an easy-to-clean-up session, not a no-write ephemeral mode; content exists
  in the database until the manual delete.
- **Persistent side effects are blocked while active.** Memory writes, task
  and task-draft creation, task plans, calendar mutations, RSS/source
  mutations, configured JSON API calls, and workspace/file writes are removed
  from the offered tool surface and hard-blocked at dispatch. Task-draft
  accept/deny endpoints reject incognito sessions.
- **Minimal metadata is retained.** LLM usage accounting keeps token counts,
  model, and timing (never prompt/completion bodies). Tool audit rows keep
  the tool name and session id but redact arguments and results for
  incognito sessions. Blocked tool attempts are logged by name only.
- **Not a host-level privacy feature.** Server process logs, database
  backups taken while the session is active, and anything outside the app
  boundary are not scrubbed.

Incognito creation is limited to `admin_roles`.

## Minimum Deployment Checklist

- [ ] `JWT_SECRET_KEY` changed
- [ ] seeded passwords changed or seed users removed
- [ ] `.env` not committed
- [ ] database not exposed publicly
- [ ] backend protected by trusted network or reverse proxy
- [ ] only required MCP servers enabled
- [ ] developer-only MCPs remain disabled in the default deployment
- [ ] default shared skills reviewed and documented
- [ ] APNs/calendar credentials stored outside the repo
- [ ] admin accounts reviewed
- [ ] backup/restore plan for PostgreSQL and uploaded files documented

## Credential Maintenance

Stop the backend, including its reloader, before coordinated credential changes.
Leave PostgreSQL running. Back up `.env`, database role definitions and the
database into an owner-only directory excluded from Git. These backups contain
secrets and must receive the same protection as the live configuration.

`.env` should be mode `0600`. Use a password manager for login passwords, and
never put plaintext credentials in command arguments, committed YAML, tickets,
logs or chat messages. The local operator reset command is:

```bash
.venv/bin/python scripts/manage_passwords.py --username admin
```

It requires a working terminal for hidden input and confirmation and at least
16 characters. An intentional generated reset can instead use `--output-file`
with a **new** private JSON path. `--rotate-defaults --output-file NEW_PATH`
changes only accounts still matching the historical seeded default; existing
nondefault passwords, account IDs, permissions, activity flags and owned data
are preserved. Recovery files use exclusive creation and mode `0600` and are
written before the database commit. If the command reports failure, the file is
not proof that the password transaction committed. Existing JWTs are not revoked
by password reset alone: rotate `JWT_SECRET_KEY` and restart when all existing
logins must be invalidated. JWT rotation also invalidates pending OAuth state;
restart any in-progress account connection flow and renew saved MCP auth tokens.

For enrolled installations, use the [database-role maintenance contract](database_roles.md):
the API uses `fruitcake_app`, Alembic uses `fruitcake_migrator`, and the retained
`fruitcake` administrator is operator-only. `DB_PASSWORD` no longer belongs in
normal API configuration. Coordinate password changes with the protected role
files and runtime URLs while the backend is stopped; verify a new TCP connection
succeeds and the old password fails. `POSTGRES_PASSWORD_FILE` initializes only new
data volumes; changing Compose secrets alone does not rotate an existing role.
Never remove the data volume for password maintenance. Legacy installations must
explicitly enroll; installing this code does not silently change their roles.

Do not casually replace `SECRETS_MASTER_KEY`: existing integration credentials
are encrypted with it. Keep it separate from `JWT_SECRET_KEY`, retain it securely,
and use a deliberate decrypt/re-encrypt migration for any future rotation.

New seeded accounts receive independent generated passwords saved privately in
`storage/.bootstrap_credentials/`. The directory is hidden so linked-folder
ingestion skips it. Keep operator recovery files outside linked-source roots or
in hidden directories as well: Git exclusion and mode `0600` alone do not prevent
the application, running as the same OS user, from reading them.
Seeding skips existing accounts without resetting
them. Suspend unused accounts through Settings > Users & Access after reviewing
which identities own tasks and data; do not delete identities just to remove
their default passwords.

### Local Rollout (2026-10-18)

The backend was stopped for fresh database, role and configuration backups under
the Git-ignored, owner-only directory `storage/.credential-hardening-20261018-195804/`.
Five accounts (`admin`, `guest`, `kid`, `parent`, `restricted`) received independent
generated passwords in `new-login-passwords.json` in that directory. The two
accounts already using nondefault passwords were unchanged. No accounts were
deleted or suspended and no roles or ownership IDs changed.

The PostgreSQL role password and all three matching `.env` database settings
were rotated together; a new connection succeeded and the old password failed.
`JWT_SECRET_KEY` was independently rotated; old-key access and refresh signatures
are rejected. `.env` remains mode `0600`. `SECRETS_MASTER_KEY` and all three
encrypted secret records were unchanged and each saved credential still decrypted.
All 51 public-table row counts matched before normal backend activity resumed.
No newly created recovery files were indexed into the document library.

Normal startup initially exposed a historical hardcoded database URL in Alembic.
Migrations now use the application's configured async `DATABASE_URL` in online
and offline modes, without ConfigParser interpolation of encoded passwords.
The committed INI no longer contains database credentials. Normal `start.sh`
then passed migrations, skipped existing seed accounts and completed startup;
the PostgreSQL container environment now matches the rotated password.

Live HTTP checks passed health, new admin login, authenticated account access,
default-password rejection and anonymous-registration rejection. The focused
credential/auth/access/integration/bootstrap suite passed 195 tests. Real
registration authorization is tested without the legacy test fixtures' narrow
registration-only dependency override. No fully green broad-suite claim is made.
All existing clients must sign in again; stored Fruitcake MCP bearer tokens must
also be renewed. FieldKit remains unavailable as in the earlier runtime rollout.

## Status

Security baseline maturity: **good for local-first trusted deployments; not yet positioned as hardened for hostile public hosting**.

That boundary is intentional and should remain explicit until the deployment model, rate limiting, signed webhooks, and broader data-governance story are pushed further.
