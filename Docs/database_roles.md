# Database Role Separation

Fruitcake's application account roles (admin/parent/etc.) are unchanged. These
PostgreSQL identities instead separate runtime data access from maintenance:

| Identity | Use | Authority |
| --- | --- | --- |
| `fruitcake_app` | API, scheduler, seed and ordinary tools | CONNECT, public-schema USAGE, table CRUD, sequence USAGE/SELECT |
| `fruitcake_migrator` | Explicit Alembic subprocess | Owns application tables and public schema; no superuser, role/database creation or privileged role membership |
| `fruitcake` | Operator recovery/provisioning | Existing database/extension owner and PostgreSQL superuser; never the enrolled API connection |

Runtime has no schema CREATE, TEMP, TRUNCATE, sequence reset, role switching to
maintenance identities, server-file/program access or migration-ledger writes.
Default grants cover tables/sequences created by the migrator. Alembic protects
its version table after each online migration. pgvector remains administrator
owned, and API startup checks availability instead of installing extensions.

## Enroll An Existing Database

Back up `.env`, database globals and a full logical database dump in an owner-only,
Git-ignored, non-ingested directory. Verify a restore in an isolated database.
Stop the API **and reloader**, but leave PostgreSQL running. Do not remove or
reuse the live volume in another running server.

```bash
.venv/bin/python scripts/database_admin.py prepare
.venv/bin/python scripts/database_admin.py provision
.venv/bin/python scripts/database_admin.py migrate upgrade head
./scripts/start.sh
```

`prepare` preserves the existing maintenance password and writes generated role
passwords privately before any database changes. `provision` changes grants and
ownership transactionally; it refuses unexpected administrative flags, role
memberships, object owners or custom routines needing an EXECUTE review. Existing
rows, app-account passwords/roles and integration encryption keys are untouched.
After successful TCP logins, `.env` is atomically changed to runtime credentials
and `DATABASE_ROLES_SEPARATED=true`; legacy `DB_PASSWORD` and migration URL keys
are removed. Backups still contain old credentials and need the same protection.

Bootstrap prepares the Docker file secret before starting PostgreSQL. An enrolled
installation does **not** reprovision on each startup. Legacy configurations
remain compatible but print an enrollment warning; they are not made safe merely
by installing this code. Separate mode fails closed if maintenance credentials
are missing, target a different database or startup detects excess privileges.

The historical migration chain assumes a pre-existing core schema. The real
integration test establishes that legacy base explicitly before applying an
actual upgrade; it is not proof of empty-database first-run onboarding. Repairing
that older onboarding limitation is separate from enrollment of an existing DB.

## Credential Handling

`storage/.database_credentials/` is mode `0700`; its two files are mode `0600`:

- `postgres-admin-password`: Docker initialization secret and operator recovery.
- `roles.json`: database-target binding and generated runtime/migrator passwords.

Paths must not be symlinks or group/world-readable. Files are created exclusively,
never silently regenerated for an enrolled installation, and remain Git-ignored.
Compose receives a file reference, not a plaintext password environment value.
`POSTGRES_PASSWORD_FILE` initializes only a new volume; changing it alone does not
rotate a password in an existing database. Do not remove the volume to rotate.

Use the wrapper for maintenance, for example:

```bash
.venv/bin/python scripts/database_admin.py migrate current
.venv/bin/python scripts/database_admin.py migrate upgrade head
```

It passes the migrator URL only in the Alembic child's environment, not in command
arguments, normal API configuration or MCP child environments. Direct Alembic
fails in separated mode without this URL. Offline SQL generation does not run
the online ledger-grant repair; reviewed offline deployments must apply that
repair explicitly. Keep maintenance output private and avoid shell tracing.

This is a SQL privilege boundary, **not OS isolation**: the local operator and
backend currently share an OS account. A fully compromised host/backend process
could read owner-accessible private files. A separate service account/secret
broker is needed for that stronger threat model. The app still legitimately has
CRUD access to its data; this does not add row-level isolation or replace app
authorization.

## Recovery And Verification

Preserve both private files together with the protected configuration and database
backups. If provisioning commits but the configuration write fails, keep the API
stopped and rerun provisioning with those same files. It is repeatable and does
not generate new passwords. If credentials are missing, restore them; do not
delete the directory or change the separated flag to bypass the failure.

For emergency rollback, while the API is stopped, restore the protected pre-change
`.env` and restart with the retained maintenance identity. The database/table
ownership need not be reverted for that superuser recovery connection. This is a
temporary privileged fallback, not a normal runtime mode. No data-volume deletion
is involved. Restore a database backup only for actual data recovery.

Compare all table row counts before/after, check role flags, PUBLIC permissions,
memberships, table/extension ownership and Alembic head. Check login, chat, tasks,
memory, RAG and coding through the enrolled runtime. The opt-in test exercises
actual PostgreSQL password logins, CRUD/vector use, denied operations, future
grants and a non-superuser migration:

```bash
FRUITCAKE_TEST_DOCKER=1 .venv/bin/python -m pytest -q tests/test_database_roles.py
```

The test creates/removes only a uniquely named disposable container and its own
anonymous volume. CI uses the freshly built reviewed PostgreSQL image.
