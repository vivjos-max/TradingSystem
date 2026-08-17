from __future__ import annotations

import os
from urllib.parse import quote

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config
if url := os.getenv("DATABASE_URL"):
    config.set_main_option(
        "sqlalchemy.url",
        url.replace("postgresql://", "postgresql+psycopg://", 1).replace("%", "%%"),
    )
elif os.getenv("POSTGRES_HOST"):
    user = quote(os.getenv("POSTGRES_USER", "trading_system"), safe="")
    password = quote(os.getenv("POSTGRES_PASSWORD", ""), safe="")
    host = os.environ["POSTGRES_HOST"]
    port = os.getenv("POSTGRES_PORT", "5432")
    database = quote(os.getenv("POSTGRES_DB", "trading_system"), safe="")
    config.set_main_option(
        "sqlalchemy.url",
        f"postgresql+psycopg://{user}:{password}@{host}:{port}/{database}".replace("%", "%%"),
    )


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_offline() if context.is_offline_mode() else run_migrations_online()
