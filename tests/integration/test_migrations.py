"""Alembic migrations apply cleanly and match the ORM models (no drift)."""

from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from alembic import command
from app.db.base import Base


def test_upgrade_head_matches_models_and_downgrades(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'migrate.db'}"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")

    sync_engine = create_engine(f"sqlite:///{tmp_path / 'migrate.db'}")
    with sync_engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
        assert set(Base.metadata.tables) <= tables
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": False}), Base.metadata)
        assert diff == [], diff

    command.downgrade(cfg, "base")
    with sync_engine.connect() as conn:
        assert set(inspect(conn).get_table_names()) <= {"alembic_version"}
    sync_engine.dispose()
