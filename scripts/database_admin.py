#!/usr/bin/env python3
"""Operator-only database provisioning and credential-isolated migrations."""

import argparse
import json
import os
from pathlib import Path
import secrets
import shlex
import stat
import subprocess
import sys
import tempfile

from dotenv import dotenv_values
import psycopg2
from psycopg2 import sql
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
APP_ROLE = "fruitcake_app"
MIGRATION_ROLE = "fruitcake_migrator"
ADMIN_ROLE = "fruitcake"


def private_path(path, *, directory=False):
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Database credential paths must be owner-only, non-symlink files/directories.")


def write_private(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def read_env(path):
    private_path(path)
    values = dotenv_values(path, interpolate=False)
    url = make_url(values["DATABASE_URL"])
    if url.get_backend_name() != "postgresql" or not url.host or not url.database:
        raise ValueError("Maintenance requires an explicit PostgreSQL host and database.")
    return values, url


def binding(url):
    return {"host": url.host, "port": url.port or 5432, "database": url.database}


def prepare(env_file, directory):
    values, url = read_env(env_file)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_path(directory, directory=True)
    admin_file = directory / "postgres-admin-password"
    roles_file = directory / "roles.json"
    enrolled = values.get("DATABASE_ROLES_SEPARATED", "").lower() == "true" and url.password != "provision-on-first-start"
    if enrolled and (not admin_file.exists() or not roles_file.exists()):
        raise ValueError("Enrolled credentials are missing; restore the private files before continuing.")
    if not admin_file.exists():
        if url.username == ADMIN_ROLE:
            password = url.password or values.get("DB_PASSWORD")
            if not password:
                raise ValueError("Existing administrator password is required for enrollment.")
        elif roles_file.exists():
            raise ValueError("Administrator recovery file is missing; restore it before continuing.")
        else:
            password = secrets.token_urlsafe(36)
        write_private(admin_file, password)
    private_path(admin_file)
    if not roles_file.exists():
        write_private(roles_file, json.dumps({
            "target": binding(url),
            "app_password": secrets.token_urlsafe(36),
            "migration_password": secrets.token_urlsafe(36),
        }) + "\n")
    private_path(roles_file)
    credentials = json.loads(roles_file.read_text())
    if credentials["target"] != binding(url):
        raise ValueError("Private credentials belong to a different database; refusing to reuse them.")
    return values, url, credentials, admin_file.read_text()


def connect(url, username, password):
    return psycopg2.connect(
        host=url.host, port=url.port or 5432, dbname=url.database,
        user=username, password=password, connect_timeout=10,
        application_name="fruitcake_database_maintenance",
    )


def provision_sql(connection, credentials):
    """Transactional enrollment; the administrator retains extension ownership."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user, rolsuper FROM pg_roles WHERE rolname=current_user")
        if cursor.fetchone() != (ADMIN_ROLE, True):
            raise ValueError("Provisioning requires the existing fruitcake maintenance administrator.")
        # Password DDL must not be copied into ordinary PostgreSQL statement logs.
        cursor.execute("SET LOCAL log_statement='none'; SET LOCAL log_min_error_statement='panic'; SET LOCAL log_min_duration_statement=-1; SET LOCAL log_duration=off; SET LOCAL log_min_duration_sample=-1")
        for role, key in ((APP_ROLE, "app_password"), (MIGRATION_ROLE, "migration_password")):
            cursor.execute("SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname=%s", (role,))
            flags = cursor.fetchone()
            if flags and any(flags):
                raise ValueError("Managed role already has unexpected administrative privileges.")
            cursor.execute("SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member WHERE r.rolname=%s", (role,))
            if cursor.fetchone():
                raise ValueError("Managed role has unexpected memberships.")
            if flags is None:
                cursor.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
            cursor.execute(sql.SQL("ALTER ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %s").format(sql.Identifier(role)), (credentials[key],))
        cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cursor.execute("SELECT pg_get_userbyid(extowner) FROM pg_extension WHERE extname='vector'")
        if cursor.fetchone()[0] != ADMIN_ROLE:
            raise ValueError("pgvector has an unexpected owner; review before enrollment.")
        cursor.execute("SELECT current_database()")
        database = sql.Identifier(cursor.fetchone()[0])
        cursor.execute("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=current_database()")
        if cursor.fetchone()[0] != ADMIN_ROLE:
            raise ValueError("The database has an unexpected owner; review before enrollment.")
        cursor.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC, fruitcake_app, fruitcake_migrator").format(database))
        cursor.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO fruitcake_app, fruitcake_migrator").format(database))
        cursor.execute("ALTER SCHEMA public OWNER TO fruitcake_migrator")
        cursor.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC, fruitcake_app; GRANT USAGE ON SCHEMA public TO fruitcake_app")
        cursor.execute("""
            SELECT c.relkind, c.relname, r.rolname FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_roles r ON r.oid=c.relowner
            WHERE n.nspname='public' AND c.relkind IN ('r','p','S','v','m','f')
              AND NOT EXISTS(SELECT 1 FROM pg_depend d WHERE d.classid='pg_class'::regclass AND d.objid=c.oid AND d.deptype='e')
            ORDER BY c.relkind='S', c.relname
        """)
        for kind, name, owner in cursor.fetchall():
            if owner not in (ADMIN_ROLE, MIGRATION_ROLE):
                raise ValueError("Application object has an unexpected owner; review before provisioning.")
            command = {"S": "SEQUENCE", "v": "VIEW", "m": "MATERIALIZED VIEW", "f": "FOREIGN TABLE"}.get(kind, "TABLE")
            cursor.execute(sql.SQL("ALTER {} public.{} OWNER TO fruitcake_migrator").format(sql.SQL(command), sql.Identifier(name)))
        cursor.execute("""
            SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
            WHERE n.nspname='public' AND t.typtype IN ('d','e')
              AND NOT EXISTS(SELECT 1 FROM pg_depend d WHERE d.classid='pg_type'::regclass AND d.objid=t.oid AND d.deptype='e')
        """)
        for (name,) in cursor.fetchall():
            cursor.execute(sql.SQL("ALTER TYPE public.{} OWNER TO fruitcake_migrator").format(sql.Identifier(name)))
        cursor.execute("""
            SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='public' AND NOT EXISTS(
                SELECT 1 FROM pg_depend d WHERE d.classid='pg_proc'::regclass AND d.objid=p.oid AND d.deptype='e') LIMIT 1
        """)
        if cursor.fetchone():
            raise ValueError("Custom routines need an explicit ownership/EXECUTE review before enrollment.")
        cursor.execute("""
            REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC, fruitcake_app;
            GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO fruitcake_app;
            REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC, fruitcake_app;
            GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO fruitcake_app;
            ALTER DEFAULT PRIVILEGES FOR ROLE fruitcake_migrator REVOKE ALL ON TABLES FROM PUBLIC, fruitcake_app;
            ALTER DEFAULT PRIVILEGES FOR ROLE fruitcake_migrator REVOKE ALL ON SEQUENCES FROM PUBLIC, fruitcake_app;
            ALTER DEFAULT PRIVILEGES FOR ROLE fruitcake_migrator IN SCHEMA public REVOKE ALL ON TABLES FROM PUBLIC, fruitcake_app;
            ALTER DEFAULT PRIVILEGES FOR ROLE fruitcake_migrator IN SCHEMA public REVOKE ALL ON SEQUENCES FROM PUBLIC, fruitcake_app;
            ALTER DEFAULT PRIVILEGES FOR ROLE fruitcake_migrator IN SCHEMA public
                GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO fruitcake_app;
            ALTER DEFAULT PRIVILEGES FOR ROLE fruitcake_migrator IN SCHEMA public
                GRANT USAGE, SELECT ON SEQUENCES TO fruitcake_app;
        """)
        cursor.execute("""
            DO $$ BEGIN IF to_regclass('public.alembic_version') IS NOT NULL THEN
                REVOKE ALL ON public.alembic_version FROM fruitcake_app;
                GRANT SELECT ON public.alembic_version TO fruitcake_app;
            END IF; END $$;
        """)


def rewrite_env(env_file, url, credentials):
    updates = {
        "DATABASE_URL": url.set(drivername="postgresql+asyncpg", username=APP_ROLE, password=credentials["app_password"]).render_as_string(hide_password=False),
        "DATABASE_URL_SYNC": url.set(drivername="postgresql+psycopg2", username=APP_ROLE, password=credentials["app_password"]).render_as_string(hide_password=False),
        "DATABASE_ROLES_SEPARATED": "true",
    }
    lines = []
    for line in env_file.read_text().splitlines():
        key = line.strip().removeprefix("export ").split("=", 1)[0].strip()
        if key not in {*updates, "DB_PASSWORD", "DATABASE_MIGRATION_URL"}:
            lines.append(line)
    lines.extend(f"{key}={shlex.quote(value)}" for key, value in updates.items())
    fd, temporary = tempfile.mkstemp(prefix=".env-roles-", dir=env_file.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write("\n".join(lines) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, env_file)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def provision(env_file, directory):
    _, url, credentials, admin_password = prepare(env_file, directory)
    with connect(url, ADMIN_ROLE, admin_password) as connection:
        # Do not take over identically named roles used by another installation.
        with connection.cursor() as cursor:
            cursor.execute("SELECT rolname FROM pg_roles WHERE rolname IN (%s, %s)", (APP_ROLE, MIGRATION_ROLE))
            existing = {row[0] for row in cursor.fetchall()}
        for role, key in ((APP_ROLE, "app_password"), (MIGRATION_ROLE, "migration_password")):
            if role in existing:
                probe = connect(url, role, credentials[key])
                probe.close()
        provision_sql(connection, credentials)
    # Validate password-based TCP connections before replacing the runtime URL.
    for role, key in ((APP_ROLE, "app_password"), (MIGRATION_ROLE, "migration_password")):
        with connect(url, role, credentials[key]) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_user")
                assert cursor.fetchone()[0] == role
    rewrite_env(env_file, url, credentials)


def migrate(env_file, directory, arguments):
    values, url = read_env(env_file)
    environment = os.environ.copy()
    environment.pop("DB_PASSWORD", None)
    environment.pop("DATABASE_MIGRATION_URL", None)
    for key in ("DATABASE_URL", "DATABASE_URL_SYNC", "DATABASE_ROLES_SEPARATED"):
        if key in values:
            environment[key] = values[key]
    if values.get("DATABASE_ROLES_SEPARATED", "").lower() == "true":
        private_path(directory, directory=True)
        path = directory / "roles.json"
        private_path(path)
        credentials = json.loads(path.read_text())
        if credentials["target"] != binding(url):
            raise ValueError("Migration credentials target a different database.")
        environment["DATABASE_MIGRATION_URL"] = url.set(
            drivername="postgresql+asyncpg", username=MIGRATION_ROLE,
            password=credentials["migration_password"],
        ).render_as_string(hide_password=False)
    return subprocess.run([sys.executable, "-m", "alembic", *arguments], cwd=ROOT, env=environment).returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--credentials-dir", type=Path, default=ROOT / "storage/.database_credentials")
    parser.add_argument("command", choices=("prepare", "provision", "bootstrap", "migrate"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if args.command == "migrate":
            return migrate(args.env_file, args.credentials_dir, args.arguments)
        if args.command == "prepare":
            prepare(args.env_file, args.credentials_dir)
        elif args.command == "provision":
            provision(args.env_file, args.credentials_dir)
        else:
            values, url = read_env(args.env_file)
            if values.get("DATABASE_ROLES_SEPARATED", "").lower() != "true":
                print("Legacy database role mode: run database_admin.py provision during a maintenance window.")
            elif url.password == "provision-on-first-start":
                provision(args.env_file, args.credentials_dir)
        print("Database maintenance step completed (credentials not displayed).")
        return 0
    except Exception as error:
        # Driver errors can include URLs, SQL and passwords; never echo them.
        print(f"Database maintenance failed ({type(error).__name__}). Check private credential permissions, database target and role ownership; see Docs/database_roles.md.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
