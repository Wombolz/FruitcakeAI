# Security Baseline

This document describes the current security baseline for FruitcakeAI as of release `v0.6.7`.

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
3. Require explicit approval before irreversible actions when task approval is enabled.
4. Degrade safely when local dependencies fail instead of continuing in an unknown state.
5. Treat optional integrations as additive, not load-bearing.

This baseline assumes a trusted local network or a deliberately hardened reverse-proxy/front-door. FruitcakeAI does not currently claim internet-exposed, zero-trust readiness by default.

## Current Security Controls

### Authentication and authorization

- JWT-based authentication for API access.
- Role-based authorization with current roles:
  - `admin`
  - `parent`
  - `restricted`
  - `guest`
- Admin endpoints are protected by `require_admin`.
- Inactive or missing users are rejected at the auth dependency layer.

### Local-first operation

- Local Ollama is the default LLM backend.
- Cloud LLMs are opt-in via environment configuration.
- If no cloud API keys are configured, no LLM traffic leaves the machine.

### Data isolation and scoped behavior

- Per-user task ownership and chat session ownership are enforced in API routes.
- Persona-scoped blocked tools and content restrictions are applied in agent context.
- Skills are additive only and cannot bypass persona-blocked tools or task execution-profile caps.
- Memory retrieval is per-user and deliberate recall/material-use is tracked separately from passive retrieval.

### Task and tool safety

- Approval gating exists for irreversible tools when task approval is enabled.
- Scheduler guardrails pause and requeue on local LLM unavailability instead of repeatedly churning failed runs.
- Run diagnostics and admin inspection surfaces make tool use, artifacts, and skill injection observable.

### Transport and secrets handling

- APNs uses token-based authentication with the Apple `.p8` key supplied by the operator.
- Sensitive settings are expected via `.env` / environment variables, not hardcoded in the repo.
- MCP integrations are explicitly configured; unused integrations are not automatically active.

### Persistence and auditability

- Task runs are persisted separately from tasks.
- Task run artifacts preserve prepared datasets, draft/final output, validation reports, and run diagnostics.
- Audit logs persist tool-call traces for admin review.
- Memory deletes are soft deletes, preserving history unless explicitly redesigned later.

## Operator Responsibilities

These are mandatory for any deployment beyond personal local testing.

### Required before shared-network use

1. Change all default seeded passwords from `config/users.yaml`.
2. Change `SECRET_KEY` / `jwt_secret_key` to a unique, strong value.
3. Keep PostgreSQL bound to trusted interfaces only, or place it behind host-level firewall rules.
4. Do not expose the API directly to the public internet without TLS and an authenticated reverse proxy.
5. Restrict filesystem access to `.env`, APNs keys, and any calendar credentials.

### Strongly recommended

1. Run behind a reverse proxy that provides TLS, request size limits, and basic abuse controls.
2. Limit inbound access to trusted devices or a VPN if remote access is needed.
3. Rotate any compromised JWT secret, APNs key, or cloud API key immediately.
4. Review enabled MCP servers and remove any capability you are not actively using.
5. Review admin accounts and keep the number of admin users minimal.

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
- `/admin/audit`
- `/admin/task-runs`
- `/admin/task-runs/{id}/inspect`
- `/admin/tools`
- `/admin/mcp/diagnostics`
- `/admin/skills/*`

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

## Security-Sensitive Defaults and Files

- `.env`
- `config/users.yaml`
- `config/mcp_config.yaml`
- APNs `.p8` auth key referenced by `APNS_AUTH_KEY_PATH`
- any Google Calendar service account credentials
- any Apple CalDAV app password

These should never be committed with live secrets.

## Update and Vulnerability Hygiene

Current practice in the repo:

- Python runtime upgraded to 3.11
- recent security-driven dependency updates were merged before `v0.6.7`
- skills install path now enforces preview/install invariants and converts DB uniqueness conflicts into clean `409` responses

Recommended ongoing hygiene:

1. Run `pip-audit` periodically.
2. Re-run `pip check` after dependency changes.
3. Review MCP server updates before enabling new images or configs.
4. Revisit this baseline whenever a new public integration surface is added.

## Minimum Deployment Checklist

- [ ] `SECRET_KEY` changed
- [ ] seeded passwords changed or seed users removed
- [ ] `.env` not committed
- [ ] database not exposed publicly
- [ ] backend protected by trusted network or reverse proxy
- [ ] only required MCP servers enabled
- [ ] APNs/calendar credentials stored outside the repo
- [ ] admin accounts reviewed
- [ ] backup/restore plan for PostgreSQL and uploaded files documented

## Status

Security baseline maturity: **good for local-first trusted deployments; not yet positioned as hardened for hostile public hosting**.

That boundary is intentional and should remain explicit until the deployment model, rate limiting, signed webhooks, and broader data-governance story are pushed further.
