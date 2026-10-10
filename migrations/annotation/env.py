"""Alembic environment for the annotation schema. Run as a deployment step, never at API startup."""
from alembic import context
from sqlalchemy import create_engine, text

from apps.api.app.annotation.config import settings


def run_migrations_online() -> None:
    engine = create_engine(settings.sqlalchemy_url)
    with engine.connect() as connection:
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS annotation"))
        connection.commit()
        context.configure(connection=connection, version_table_schema="annotation", transactional_ddl=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("Offline SQL generation is not supported; run against a database.")
run_migrations_online()
