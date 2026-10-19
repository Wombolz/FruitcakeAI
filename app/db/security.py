"""Read-only startup checks and explicit migration-connection selection."""
import os

from sqlalchemy import text
from sqlalchemy.engine import make_url


def migration_url(settings):
    url = os.environ.get("DATABASE_MIGRATION_URL")
    if settings.database_roles_separated:
        if not url or make_url(url).username != "fruitcake_migrator":
            raise RuntimeError("Separate migration credentials required; use scripts/database_admin.py migrate.")
        runtime, migration = make_url(settings.database_url), make_url(url)
        if (runtime.host, runtime.port or 5432, runtime.database) != (migration.host, migration.port or 5432, migration.database):
            raise RuntimeError("Migration credentials target a different database.")
    return url or settings.database_url


async def check_database(connection, *, separated):
    if connection.dialect.name != "postgresql":
        return
    version = await connection.scalar(text("SELECT extversion FROM pg_extension WHERE extname='vector'"))
    if not version:
        raise RuntimeError("pgvector is missing; provision it through database maintenance before starting the API.")
    if separated:
        row = (await connection.execute(text("""
            SELECT current_user AS name, r.rolsuper, r.rolcreatedb, r.rolcreaterole,
                   r.rolreplication, r.rolbypassrls,
                   has_database_privilege(current_user, current_database(), 'CREATE') AS database_create,
                   has_database_privilege(current_user, current_database(), 'TEMP') AS temporary,
                   has_schema_privilege(current_user, 'public', 'CREATE') AS schema_create,
                   EXISTS(SELECT 1 FROM pg_auth_members WHERE member=r.oid) AS membership,
                   EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                          WHERE c.relowner=r.oid AND n.nspname='public') AS owns_objects,
                   EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                          WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','f')
                          AND CASE WHEN c.relkind IN ('r','p','v','m','f') THEN
                              has_table_privilege(r.oid,c.oid,'TRUNCATE,REFERENCES,TRIGGER') ELSE false END) AS elevated_table_grants,
                   EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                          WHERE n.nspname='public' AND c.relkind='S'
                          AND CASE WHEN c.relkind='S' THEN
                              has_sequence_privilege(r.oid,c.oid,'UPDATE') ELSE false END) AS sequence_reset,
                   COALESCE(has_table_privilege(r.oid,to_regclass('public.alembic_version'),
                            'INSERT,UPDATE,DELETE'),false) AS migration_write
            FROM pg_roles r WHERE r.rolname=current_user
        """))).mappings().one()
        if row["name"] != "fruitcake_app" or any(value for key, value in row.items() if key != "name"):
            raise RuntimeError("Runtime database role is overprivileged; repair it through database maintenance.")


def protect_migration_ledger(connection):
    connection.execute(text("""
        DO $$ BEGIN
            IF to_regclass('public.alembic_version') IS NOT NULL THEN
                REVOKE ALL ON public.alembic_version FROM fruitcake_app;
                GRANT SELECT ON public.alembic_version TO fruitcake_app;
            END IF;
        END $$;
    """))
