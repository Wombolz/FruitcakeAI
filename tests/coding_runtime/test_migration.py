from importlib import import_module

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

from app.db.session import Base
import app.db.models  # Register expected coding metadata.


TABLES = ("coding_workspaces", "coding_sessions", "coding_runs", "coding_run_events", "coding_approvals")


def test_coding_migration_matches_orm_and_downgrades_cleanly(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.sqlite'}")
    migration = import_module("app.db.migrations.versions.052_coding_sessions")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE user_projects (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE model_profiles (id INTEGER PRIMARY KEY)")
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                migration.upgrade()
                inspector = inspect(connection)
                for name in TABLES:
                    expected = set(Base.metadata.tables[name].columns.keys())
                    if name == "coding_sessions":
                        expected -= {"edit_mode", "command_mode", "archived_at", "deleted_at"}
                    if name == "coding_runs":
                        expected -= {"model_profile_id", "model_id"}
                    assert {column["name"] for column in inspector.get_columns(name)} == expected
                    assert {index["name"] for index in inspector.get_indexes(name)} == {
                        index.name for index in Base.metadata.tables[name].indexes
                    }
                    assert len(inspector.get_foreign_keys(name)) == len(Base.metadata.tables[name].foreign_key_constraints) - (1 if name == "coding_runs" else 0)
                migration.downgrade()
                assert not set(TABLES).intersection(inspect(connection).get_table_names())
    finally:
        engine.dispose()


def test_checkout_migration_matches_orm_and_preserves_prior_tables(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'checkouts.sqlite'}")
    first = import_module("app.db.migrations.versions.052_coding_sessions")
    migration = import_module("app.db.migrations.versions.053_coding_checkouts")
    try:
        with engine.begin() as connection:
            for table in ("users", "user_projects", "model_profiles"):
                connection.exec_driver_sql(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
            with Operations.context(MigrationContext.configure(connection)):
                first.upgrade()
                migration.upgrade()
                inspector = inspect(connection)
                expected = Base.metadata.tables["coding_checkouts"]
                assert {column["name"] for column in inspector.get_columns(expected.name)} == set(expected.columns.keys()) - {"workspace_id", "head_commit", "branch_name"}
                assert len(inspector.get_foreign_keys(expected.name)) == len(expected.foreign_key_constraints) - 1
                assert {tuple(constraint["column_names"]) for constraint in inspector.get_unique_constraints(expected.name)} == {
                    ("session_id",), ("canonical_path",),
                }
                migration.downgrade()
                assert set(TABLES).issubset(inspect(connection).get_table_names())
    finally:
        engine.dispose()
