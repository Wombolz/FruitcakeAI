"""Credential isolation, fail-closed startup, and opt-in real PostgreSQL grants."""

import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import time
import uuid

import psycopg2
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.security import check_database, migration_url, protect_migration_ledger
from scripts import database_admin as admin


def env_file(path, url):
    path.write_text(f"DATABASE_URL={url}\nJWT_SECRET_KEY=keep-this-key\nSECRETS_MASTER_KEY=keep-this-too\nDB_PASSWORD=legacy\n")
    path.chmod(0o600)
    return path


def test_credentials_are_private_stable_and_target_bound(tmp_path):
    env = env_file(tmp_path / ".env", "postgresql+asyncpg://fruitcake:old%25secret@localhost/example")
    directory = tmp_path / "private"
    first = admin.prepare(env, directory)
    assert first[3] == "old%secret"
    assert admin.prepare(env, directory)[2:] == first[2:]
    assert directory.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in directory.iterdir())
    env.write_text(env.read_text().replace("/example", "/other"))
    with pytest.raises(ValueError, match="different database"):
        admin.prepare(env, directory)


def test_private_paths_refuse_symlinks_and_broad_permissions(tmp_path):
    target = tmp_path / "secret"
    admin.write_private(target, "original")
    with pytest.raises(FileExistsError):
        admin.write_private(target, "replacement")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ValueError):
        admin.private_path(link)
    target.chmod(0o644)
    with pytest.raises(ValueError):
        admin.private_path(target)


def test_env_rewrite_preserves_app_secrets_and_removes_maintenance_keys(tmp_path):
    env = env_file(tmp_path / ".env", "postgresql+asyncpg://fruitcake:old@localhost/example")
    env.write_text(env.read_text() + "export DATABASE_MIGRATION_URL=must-not-remain\n# keep comment\n")
    admin.rewrite_env(env, make_url(admin.read_env(env)[1]), {"app_password": "new%password"})
    values, url = admin.read_env(env)
    assert values["JWT_SECRET_KEY"] == "keep-this-key"
    assert values["SECRETS_MASTER_KEY"] == "keep-this-too"
    assert "DB_PASSWORD" not in values and "DATABASE_MIGRATION_URL" not in values
    assert url.username == "fruitcake_app" and url.password == "new%password"
    assert values["DATABASE_ROLES_SEPARATED"] == "true"
    assert "# keep comment" in env.read_text()
    assert env.stat().st_mode & 0o777 == 0o600


def test_missing_enrolled_credentials_cannot_be_silently_replaced(tmp_path):
    env = env_file(tmp_path / ".env", "postgresql+asyncpg://fruitcake_app:existing@localhost/example")
    env.write_text(env.read_text() + "DATABASE_ROLES_SEPARATED=true\n")
    directory = tmp_path / "private"
    with pytest.raises(ValueError, match="restore"):
        admin.prepare(env, directory)
    assert not (directory / "roles.json").exists()


def test_fresh_template_gets_generated_not_default_passwords(tmp_path):
    env = env_file(tmp_path / ".env", "postgresql+asyncpg://fruitcake_app:provision-on-first-start@localhost/example")
    env.write_text(env.read_text() + "DATABASE_ROLES_SEPARATED=true\n")
    _, _, credentials, administrator = admin.prepare(env, tmp_path / "private")
    assert len(administrator) >= 32
    assert len(credentials["app_password"]) >= 32
    assert len(credentials["migration_password"]) >= 32
    assert len({administrator, credentials["app_password"], credentials["migration_password"]}) == 3


def test_migrations_require_dedicated_credentials_and_matching_target(monkeypatch):
    settings = SimpleNamespace(database_roles_separated=True, database_url="postgresql+asyncpg://fruitcake_app:pw@localhost:5432/example")
    monkeypatch.delenv("DATABASE_MIGRATION_URL", raising=False)
    with pytest.raises(RuntimeError, match="Separate migration"):
        migration_url(settings)
    for url in (settings.database_url, "postgresql+asyncpg://fruitcake_migrator:pw@localhost:5432/other"):
        monkeypatch.setenv("DATABASE_MIGRATION_URL", url)
        with pytest.raises(RuntimeError):
            migration_url(settings)
    url = "postgresql+asyncpg://fruitcake_migrator:encoded%25pw@localhost:5432/example"
    monkeypatch.setenv("DATABASE_MIGRATION_URL", url)
    assert migration_url(settings) == url


def test_migration_secret_is_only_in_child_environment(tmp_path, monkeypatch):
    env = env_file(tmp_path / ".env", "postgresql+asyncpg://fruitcake:old@localhost/example")
    directory = tmp_path / "private"
    _, url, credentials, _ = admin.prepare(env, directory)
    admin.rewrite_env(env, url, credentials)
    monkeypatch.setenv("DATABASE_MIGRATION_URL", "untrusted-inherited-value")
    monkeypatch.setenv("DB_PASSWORD", "inherited-admin-secret")
    calls = []
    monkeypatch.setattr(admin.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(returncode=0))
    assert admin.migrate(env, directory, ["upgrade", "head"]) == 0
    args, kwargs = calls[0]
    assert credentials["migration_password"] not in repr(args)
    assert make_url(kwargs["env"]["DATABASE_MIGRATION_URL"]).username == admin.MIGRATION_ROLE
    assert "DB_PASSWORD" not in kwargs["env"]
    assert os.environ["DATABASE_MIGRATION_URL"] == "untrusted-inherited-value"


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [None, "rolsuper", "membership", "owns_objects", "temporary", "schema_create", "database_create", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls", "elevated_table_grants", "sequence_reset", "migration_write"])
async def test_runtime_startup_rejects_effective_excess_privileges(flag):
    row = {"name": "fruitcake_app"}
    if flag:
        row[flag] = True
    connection = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    async def scalar(query):
        return "0.8.7"
    async def execute(query):
        return SimpleNamespace(mappings=lambda: SimpleNamespace(one=lambda: row))
    connection.scalar, connection.execute = scalar, execute
    if flag:
        with pytest.raises(RuntimeError, match="overprivileged"):
            await check_database(connection, separated=True)
    else:
        await check_database(connection, separated=True)


@pytest.mark.asyncio
async def test_missing_vector_fails_without_attempting_schema_changes():
    statements = []
    async def scalar(query):
        statements.append(str(query))
        return None
    connection = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), scalar=scalar)
    with pytest.raises(RuntimeError, match="pgvector is missing"):
        await check_database(connection, separated=False)
    assert len(statements) == 1 and "CREATE" not in statements[0]
    await check_database(SimpleNamespace(dialect=SimpleNamespace(name="sqlite")), separated=True)


def test_startup_uses_file_secret_and_does_not_export_admin_credentials():
    import yaml
    compose = yaml.safe_load(Path("docker-compose.yml").read_text())
    environment = compose["services"]["postgres"]["environment"]
    assert "POSTGRES_PASSWORD" not in environment
    assert environment["POSTGRES_PASSWORD_FILE"].startswith("/run/secrets/")
    source = Path("scripts/bootstrap.sh").read_text()
    assert source.index('database_admin.py" prepare') < source.index("\nstart_postgres")
    assert source.index('database_admin.py" bootstrap') < source.index("\nrun_migrations")
    assert "unset DB_PASSWORD DATABASE_MIGRATION_URL" in Path("scripts/_install_common.sh").read_text()


@contextmanager
def postgres_container(tmp_path):
    name = "fruitcake-role-test-" + uuid.uuid4().hex[:12]
    password = "disposable-test-password-only"
    image = os.environ.get("FRUITCAKE_TEST_POSTGRES_IMAGE", "fruitcake/postgres-vector:16.15-pgvector0.8.7-20261018-r1")
    password_file = tmp_path / "postgres-password"
    admin.write_private(password_file, password)
    subprocess.run([
        "docker", "run", "-d", "--name", name, "-p", "127.0.0.1::5432",
        "--mount", f"type=bind,source={password_file},target=/run/secrets/password,readonly",
        "-e", "POSTGRES_USER=fruitcake", "-e", "POSTGRES_DB=role_test",
        "-e", "POSTGRES_PASSWORD_FILE=/run/secrets/password", image,
    ], check=True, capture_output=True)
    try:
        for _ in range(90):
            if subprocess.run(["docker", "exec", name, "pg_isready", "-U", "fruitcake", "-d", "role_test"], capture_output=True).returncode == 0:
                break
            time.sleep(1)
        port = subprocess.check_output(["docker", "port", name, "5432/tcp"], text=True).strip().rsplit(":", 1)[1]
        yield make_url(f"postgresql+psycopg2://fruitcake:{password}@127.0.0.1:{port}/role_test")
    finally:
        subprocess.run(["docker", "rm", "-f", "-v", name], check=True, capture_output=True)


@pytest.mark.skipif(os.environ.get("FRUITCAKE_TEST_DOCKER") != "1", reason="opt-in disposable PostgreSQL integration")
def test_real_role_permissions_default_grants_and_alembic(tmp_path):
    with postgres_container(tmp_path) as url:
        env = env_file(tmp_path / ".env", url.render_as_string(hide_password=False))
        directory = tmp_path / "private"
        _, _, credentials, _ = admin.prepare(env, directory)
        with admin.connect(url, admin.ADMIN_ROLE, url.password) as connection:
            with connection.cursor() as cursor:
                cursor.execute("CREATE TABLE preserved(id serial PRIMARY KEY, value text); INSERT INTO preserved(value) VALUES ('unchanged')")
        admin.provision(env, directory)
        admin.provision(env, directory)  # repeatable, without rotating passwords
        runtime_url = admin.read_env(env)[1]
        migrator_url = url.set(username=admin.MIGRATION_ROLE, password=credentials["migration_password"])
        with admin.connect(url, admin.MIGRATION_ROLE, credentials["migration_password"]) as connection:
            with connection.cursor() as cursor:
                cursor.execute("CREATE TABLE future(id serial PRIMARY KEY, value text); ALTER TABLE future ADD COLUMN extra text")
        with admin.connect(url, admin.APP_ROLE, credentials["app_password"]) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT value FROM preserved")
                assert cursor.fetchone()[0] == "unchanged"
                cursor.execute("INSERT INTO future(value) VALUES ('test') RETURNING id")
                assert cursor.fetchone()[0] == 1
                cursor.execute("UPDATE future SET value='updated'; SELECT '[1,2,3]'::vector <-> '[1,2,4]'::vector")
                assert cursor.fetchone()[0] == 1
                cursor.execute("DELETE FROM future")
        for statement in (
            "CREATE TABLE forbidden(id int)", "ALTER TABLE preserved ADD COLUMN forbidden int",
            "DROP TABLE preserved", "TRUNCATE preserved", "CREATE TEMP TABLE forbidden(id int)",
            "CREATE ROLE forbidden", "CREATE DATABASE forbidden", "SET ROLE fruitcake_migrator",
            "SET ROLE fruitcake", "COPY preserved TO PROGRAM 'id'", "SELECT pg_read_file('/etc/passwd')",
            "SELECT setval('preserved_id_seq', 100)",
        ):
            connection = admin.connect(url, admin.APP_ROLE, credentials["app_password"])
            connection.autocommit = True
            try:
                with connection.cursor() as cursor, pytest.raises(psycopg2.errors.InsufficientPrivilege):
                    cursor.execute(statement)
            finally:
                connection.close()

        # Current metadata establishes the legacy pre-Alembic base in this empty fixture.
        from app.db.session import Base
        import app.db.models  # noqa: F401
        engine = create_engine(migrator_url)
        Base.metadata.create_all(engine)
        assert admin.migrate(env, directory, ["stamp", "056_coding_workspace_lifecycle"]) == 0
        assert admin.migrate(env, directory, ["upgrade", "head"]) == 0
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "057_coding_approval_wait"
            assert connection.scalar(text("SELECT pg_get_userbyid(extowner) FROM pg_extension WHERE extname='vector'")) == admin.ADMIN_ROLE
            protect_migration_ledger(connection)
            connection.commit()
        engine.dispose()
        with admin.connect(url, admin.APP_ROLE, credentials["app_password"]) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT version_num FROM alembic_version")
                assert cursor.fetchone()[0] == "057_coding_approval_wait"
                with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                    cursor.execute("UPDATE alembic_version SET version_num='corrupt'")

        async def check():
            engine = create_async_engine(runtime_url.set(drivername="postgresql+asyncpg"))
            async with engine.connect() as connection:
                await check_database(connection, separated=True)
            await engine.dispose()
        asyncio.run(check())

        unchanged = env.read_text()
        with admin.connect(url, admin.ADMIN_ROLE, url.password) as connection:
            with connection.cursor() as cursor:
                cursor.execute("CREATE FUNCTION public.unreviewed() RETURNS int LANGUAGE SQL AS 'SELECT 1'")
        with pytest.raises(ValueError, match="Custom routines"):
            admin.provision(env, directory)
        assert env.read_text() == unchanged
        with admin.connect(url, admin.ADMIN_ROLE, url.password) as connection:
            with connection.cursor() as cursor:
                cursor.execute("DROP FUNCTION public.unreviewed()")
                cursor.execute("ALTER ROLE fruitcake_app PASSWORD %s", ("mismatched-fixture-password",))
        with pytest.raises(psycopg2.OperationalError):
            admin.provision(env, directory)
        assert env.read_text() == unchanged
