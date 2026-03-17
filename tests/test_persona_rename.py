from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa

from app.agent.context import UserContext


_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "db"
    / "migrations"
    / "versions"
    / "014_rename_child_restricted.py"
)
_MIGRATION_SPEC = importlib.util.spec_from_file_location("rename_child_restricted", _MIGRATION_PATH)
assert _MIGRATION_SPEC and _MIGRATION_SPEC.loader
rename_child_restricted = importlib.util.module_from_spec(_MIGRATION_SPEC)
_MIGRATION_SPEC.loader.exec_module(rename_child_restricted)


def test_restricted_prompt_wording_is_neutral():
    ctx = UserContext(
        user_id=1,
        username="tester",
        role="restricted",
        persona="restricted_assistant",
        content_filter="strict",
    )

    prompt = ctx.to_system_prompt()

    assert "restricted-access user" in prompt
    assert "child user" not in prompt


def test_legacy_child_values_are_rewritten():
    engine = sa.create_engine("sqlite:///:memory:")

    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE users (id INTEGER PRIMARY KEY, role TEXT, persona TEXT)"))
        conn.execute(sa.text("CREATE TABLE tasks (id INTEGER PRIMARY KEY, persona TEXT)"))
        conn.execute(sa.text("CREATE TABLE chat_sessions (id INTEGER PRIMARY KEY, persona TEXT)"))
        conn.execute(
            sa.text(
                "INSERT INTO users (id, role, persona) VALUES "
                "(1, 'child', 'kids_assistant'),"
                "(2, 'parent', 'family_assistant')"
            )
        )
        conn.execute(sa.text("INSERT INTO tasks (id, persona) VALUES (1, 'kids_assistant'), (2, 'work_assistant')"))
        conn.execute(
            sa.text(
                "INSERT INTO chat_sessions (id, persona) VALUES "
                "(1, 'kids_assistant'),"
                "(2, 'family_assistant')"
            )
        )

        rename_child_restricted._apply_updates(
            conn,
            role_from="child",
            role_to="restricted",
            persona_from="kids_assistant",
            persona_to="restricted_assistant",
        )

        users = conn.execute(sa.text("SELECT id, role, persona FROM users ORDER BY id")).fetchall()
        tasks = conn.execute(sa.text("SELECT id, persona FROM tasks ORDER BY id")).fetchall()
        sessions = conn.execute(sa.text("SELECT id, persona FROM chat_sessions ORDER BY id")).fetchall()

    assert users == [
        (1, "restricted", "restricted_assistant"),
        (2, "parent", "family_assistant"),
    ]
    assert tasks == [
        (1, "restricted_assistant"),
        (2, "work_assistant"),
    ]
    assert sessions == [
        (1, "restricted_assistant"),
        (2, "family_assistant"),
    ]
