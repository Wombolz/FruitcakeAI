"""Exercise real registration authorization and local credential maintenance."""

import json
import os
import importlib.util
from contextlib import nullcontext
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
import yaml

from app.auth.jwt import create_access_token, hash_password, verify_password
from app.config import settings
from app.auth.password_admin import reset_password, rotate_default_passwords, write_private_credentials
from app.db.models import User
from app.db.session import get_db
from app.main import app
from tests.conftest import TestSessionLocal, override_get_db


@pytest.fixture
async def secured_client():
    # Deliberately do not use the legacy client fixture's registration override.
    app.dependency_overrides[get_db] = override_get_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["admin", "parent", "restricted", "guest"])
async def test_anonymous_registration_cannot_create_any_role(secured_client, role):
    response = await secured_client.post("/auth/register", json={
        "username": "intruder", "email": "intruder@example.com", "password": "secret", "role": role,
    })
    assert response.status_code == 403
    async with TestSessionLocal() as db:
        assert await db.scalar(select(func.count()).select_from(User)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["parent", "restricted", "guest", "admin"])
async def test_registration_requires_current_database_admin_role(secured_client, role):
    async with TestSessionLocal() as db:
        user = User(username="creator", email="creator@example.com", role=role, hashed_password=hash_password("creator-password"))
        db.add(user)
        await db.commit()
        uid = user.id
    # Even a forged/stale role claim must not grant administrator privileges.
    token = create_access_token(uid, "creator", "admin")
    response = await secured_client.post("/auth/register", headers={"Authorization": f"Bearer {token}"}, json={
        "username": "created", "email": "created@example.com", "password": "new-user-password", "role": "parent",
    })
    assert response.status_code == (201 if role == "admin" else 403)


def test_recovery_file_is_private_and_cannot_be_overwritten(tmp_path):
    path = tmp_path / "private" / "credentials.json"
    write_private_credentials(path, {"admin": "generated-test-password"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    with pytest.raises(FileExistsError):
        write_private_credentials(path, {"admin": "replacement"})
    assert json.loads(path.read_text()) == {"admin": "generated-test-password"}
    link = tmp_path / "symlink.json"
    link.symlink_to(path)
    with pytest.raises(FileExistsError):
        write_private_credentials(link, {"admin": "replacement"})


@pytest.mark.asyncio
async def test_rotation_only_changes_default_passwords_and_is_repeatable(tmp_path):
    async with TestSessionLocal() as db:
        db.add_all([
            User(username="default", email="default@example.com", hashed_password=hash_password("changeme123")),
            User(username="custom", email="custom@example.com", hashed_password=hash_password("existing-custom-password")),
        ])
        await db.commit()
        path = tmp_path / "logins.json"
        assert await rotate_default_passwords(db, path) == ["default"]
        await db.commit()
        passwords = json.loads(path.read_text())
        users = {u.username: u for u in (await db.execute(select(User))).scalars()}
        assert len(passwords["default"]) >= 24
        assert verify_password(passwords["default"], users["default"].hashed_password)
        assert not verify_password("changeme123", users["default"].hashed_password)
        assert verify_password("existing-custom-password", users["custom"].hashed_password)
        assert await rotate_default_passwords(db, path) == []


@pytest.mark.asyncio
async def test_unwritable_recovery_destination_prevents_password_changes(tmp_path):
    async with TestSessionLocal() as db:
        user = User(username="default", email="default@example.com", hashed_password=hash_password("changeme123"))
        db.add(user)
        await db.commit()
        path = tmp_path / "existing.json"
        path.write_text("do not replace")
        with pytest.raises(FileExistsError):
            await rotate_default_passwords(db, path)
        await db.refresh(user)
        assert verify_password("changeme123", user.hashed_password)


@pytest.mark.asyncio
async def test_local_reset_preserves_identity_and_rejects_short_password():
    async with TestSessionLocal() as db:
        user = User(username="operator", email="operator@example.com", role="admin", hashed_password=hash_password("old-password"))
        db.add(user)
        await db.commit()
        uid = user.id
        with pytest.raises(ValueError, match="16 characters"):
            await reset_password(db, "operator", "short")
        await reset_password(db, "operator", "new-long-private-password")
        await db.commit()
        assert user.id == uid and user.role == "admin"
        assert verify_password("new-long-private-password", user.hashed_password)
        assert not verify_password("old-password", user.hashed_password)


def test_cli_does_not_fall_back_to_echoing_passwords_without_a_tty():
    result = subprocess.run([sys.executable, "scripts/manage_passwords.py", "--username", "admin"],
                            input="must-not-be-read\n", capture_output=True, text=True)
    assert result.returncode == 2
    assert "interactive terminal" in result.stderr
    assert "must-not-be-read" not in result.stdout + result.stderr


def test_cli_configuration_errors_do_not_dump_environment_values(tmp_path):
    sentinel = "PRIVATE_CONFIGURATION_SENTINEL"
    result = subprocess.run([
        sys.executable, "scripts/manage_passwords.py", "--rotate-defaults", "--output-file", str(tmp_path / "credentials.json"),
    ], env={**os.environ, "APP_PORT": sentinel}, capture_output=True, text=True)
    assert result.returncode == 1
    assert "Password update failed" in result.stderr
    assert sentinel not in result.stdout + result.stderr


@pytest.mark.asyncio
async def test_seed_generates_private_credentials_and_never_resets_existing(tmp_path, monkeypatch, capsys):
    from scripts import seed
    assert seed.CREDENTIALS_DIR.name.startswith(".")
    config = tmp_path / "users.yaml"
    config.write_text(yaml.safe_dump({"users": [{"username": "fresh", "email": "fresh@example.com", "role": "admin"}]}))
    monkeypatch.setattr(seed, "CONFIG_PATH", config)
    monkeypatch.setattr(seed, "CREDENTIALS_DIR", tmp_path / "credentials")
    monkeypatch.setattr(seed, "AsyncSessionLocal", TestSessionLocal)
    await seed.seed()
    files = list((tmp_path / "credentials").glob("*.json"))
    assert len(files) == 1
    password = json.loads(files[0].read_text())["fresh"]
    assert password not in capsys.readouterr().out
    async with TestSessionLocal() as db:
        user = (await db.execute(select(User))).scalar_one()
        assert verify_password(password, user.hashed_password)
        assert not verify_password("changeme123", user.hashed_password)
        old_hash = user.hashed_password
    await seed.seed()
    assert len(list((tmp_path / "credentials").glob("*.json"))) == 1
    async with TestSessionLocal() as db:
        assert (await db.execute(select(User))).scalar_one().hashed_password == old_hash


def test_shipped_seed_configuration_has_no_fixed_passwords():
    users = yaml.safe_load(Path("config/users.yaml").read_text())["users"]
    assert all("password" not in user for user in users)


def test_doctor_has_no_default_password_or_secret_bearing_curl_arguments():
    source = Path("scripts/doctor.sh").read_text()
    assert "changeme123" not in source
    assert 'Authorization: Bearer ${admin_token}' not in source
    assert 'os.environ[\'FRUITCAKE_ADMIN_PASSWORD\']' in source
    result = subprocess.run(["bash", "-n", "scripts/doctor.sh"], capture_output=True, text=True)
    assert result.returncode == 0


@pytest.mark.parametrize("offline", [True, False])
def test_migrations_use_configured_database_url_without_ini_interpolation(monkeypatch, offline):
    from alembic import context
    from alembic.config import Config
    from sqlalchemy.ext import asyncio as sqlalchemy_asyncio

    url = "postgresql+asyncpg://operator:encoded%25password@localhost/example"
    monkeypatch.setattr(settings, "database_url", url)
    monkeypatch.setattr(settings, "database_roles_separated", False)
    monkeypatch.delenv("DATABASE_MIGRATION_URL", raising=False)
    config = Config()
    config.set_main_option("sqlalchemy.url", "postgresql+asyncpg://wrong:wrong@localhost/wrong")
    monkeypatch.setattr(context, "config", config, raising=False)
    monkeypatch.setattr(context, "is_offline_mode", lambda: offline)
    configured = []
    monkeypatch.setattr(context, "configure", lambda **kwargs: configured.append(kwargs))
    monkeypatch.setattr(context, "begin_transaction", nullcontext)
    monkeypatch.setattr(context, "run_migrations", lambda: None)
    engine_configs = []

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def run_sync(self, callback):
            callback(self)

    class Engine:
        def connect(self):
            return Connection()

        async def dispose(self):
            pass

    def factory(configuration, **kwargs):
        engine_configs.append(configuration)
        return Engine()

    monkeypatch.setattr(sqlalchemy_asyncio, "async_engine_from_config", factory)
    spec = importlib.util.spec_from_file_location("test_migration_env", "app/db/migrations/env.py")
    spec.loader.exec_module(importlib.util.module_from_spec(spec))
    if offline:
        assert configured[0]["url"] == url
    else:
        assert engine_configs[0]["sqlalchemy.url"] == url
    assert "sqlalchemy.url =" not in Path("alembic.ini").read_text()
