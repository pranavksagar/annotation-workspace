"""Engine and transaction helpers. All writes go through services using these helpers."""
from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator

from sqlalchemy import Connection, Engine, create_engine, text

from .config import settings

REQUIRED_REVISION = "0002_legacy_import_map"


@lru_cache(maxsize=None)
def get_engine(url: str | None = None) -> Engine:
    return create_engine(url or settings.sqlalchemy_url, pool_size=settings.db_pool_size,
                         max_overflow=settings.db_max_overflow, pool_pre_ping=True, future=True)


@contextmanager
def transaction(engine: Engine | None = None) -> Iterator[Connection]:
    """A short read-write transaction, committed on success and rolled back on error."""
    with (engine or get_engine()).begin() as connection:
        yield connection


@contextmanager
def snapshot_read(engine: Engine | None = None) -> Iterator[Connection]:
    """A consistent REPEATABLE READ, read-only transaction for multi-query documents."""
    with (engine or get_engine()).connect() as connection:
        connection = connection.execution_options(isolation_level="REPEATABLE READ")
        with connection.begin():
            connection.execute(text("SET TRANSACTION READ ONLY"))
            yield connection


def schema_ready(engine: Engine | None = None) -> bool:
    with (engine or get_engine()).connect() as connection:
        row = connection.execute(text(
            "SELECT to_regclass('annotation.alembic_version') IS NOT NULL")).scalar()
        if not row:
            return False
        versions = {r[0] for r in connection.execute(text("SELECT version_num FROM annotation.alembic_version"))}
        return REQUIRED_REVISION in versions  # the API expects this exact schema head
