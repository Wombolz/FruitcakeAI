"""Migration checks use disposable databases, never the configured app database."""
from importlib import import_module

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.db.session import Base
import app.db.models  # Register metadata.

TABLES = ("persona_definitions", "persona_revisions", "persona_selections", "user_persona_defaults",
          "chat_run_persona_snapshots")


def test_revision_migration_matches_models_and_only_empty_downgrade(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'personas.sqlite'}")
    migration = import_module("app.db.migrations.versions.060_persona_revisions")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE chat_sessions (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE chat_runs (id VARCHAR(64) PRIMARY KEY)")
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                inspector = inspect(connection)
                for name in TABLES:
                    expected = Base.metadata.tables[name]
                    assert {item["name"] for item in inspector.get_columns(name)} == set(expected.columns.keys())
                    assert {item["name"] for item in inspector.get_indexes(name)} == {item.name for item in expected.indexes}
                    assert len(inspector.get_foreign_keys(name)) == len(expected.foreign_key_constraints)
                    assert "CREATE TABLE" in str(CreateTable(expected).compile(dialect=postgresql.dialect()))
                connection.exec_driver_sql("INSERT INTO users VALUES (1)")
                connection.exec_driver_sql("INSERT INTO persona_definitions (id,user_id,head_version) VALUES ('owner',1,1)")
                connection.exec_driver_sql("INSERT INTO persona_revisions (id,persona_id,version,payload_json) VALUES ('rev','owner',1,'{}')")
                with pytest.raises(RuntimeError, match="refusing data loss"):
                    migration.downgrade()
                assert set(TABLES) <= set(inspect(connection).get_table_names())
                connection.exec_driver_sql("DELETE FROM users WHERE id=1")
                assert not connection.exec_driver_sql("SELECT * FROM persona_revisions").all()
                migration.downgrade()
                assert not set(TABLES) & set(inspect(connection).get_table_names())
    finally:
        engine.dispose()


def test_persona_owner_lock_does_not_conflict_with_new_chat_foreign_key_lock():
    from sqlalchemy import select
    from app.db.models import User

    sql = str(select(User).where(User.id == 7).with_for_update(key_share=True)
              .compile(dialect=postgresql.dialect()))
    assert "FOR NO KEY UPDATE" in sql


def test_visual_asset_migration_retains_data_and_matches_models(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'visual-assets.sqlite'}")
    migration = import_module("app.db.migrations.versions.061_persona_visual_assets")
    tables = ("persona_visual_assets", "persona_test_options")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE chat_sessions (id INTEGER PRIMARY KEY)")
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                for name in tables:
                    expected = Base.metadata.tables[name]
                    assert {col["name"] for col in inspect(connection).get_columns(name)} == set(expected.columns.keys())
                    assert {item["name"] for item in inspect(connection).get_indexes(name)} == {item.name for item in expected.indexes}
                    assert "CREATE TABLE" in str(CreateTable(expected).compile(dialect=postgresql.dialect()))
                connection.exec_driver_sql("INSERT INTO users VALUES (1)")
                connection.exec_driver_sql("INSERT INTO chat_sessions VALUES (1)")
                connection.exec_driver_sql("INSERT INTO persona_visual_assets (id,user_id,sha256,width,height,size_bytes,data) VALUES ('a',1,'hash',1,1,1,X'00')")
                connection.exec_driver_sql("INSERT INTO persona_test_options VALUES (1,0)")
                with pytest.raises(RuntimeError, match="refusing data loss"):
                    migration.downgrade()
                connection.exec_driver_sql("DELETE FROM users")
                assert not connection.exec_driver_sql("SELECT * FROM persona_visual_assets").all()
                connection.exec_driver_sql("DELETE FROM chat_sessions")
                assert not connection.exec_driver_sql("SELECT * FROM persona_test_options").all()
                migration.downgrade()
                assert not set(tables) & set(inspect(connection).get_table_names())
    finally:
        engine.dispose()
