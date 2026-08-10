"""Small operational commands used by the local container stack."""

from __future__ import annotations

import argparse
from pathlib import Path

from alembic import command
from alembic.config import Config

from trading_system.settings import get_settings


def migrate() -> None:
    settings = get_settings()
    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
    command.upgrade(config, "head")


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only dashboard operations")
    parser.add_argument("operation", choices=("migrate",))
    args = parser.parse_args()
    if args.operation == "migrate":
        migrate()


if __name__ == "__main__":
    main()
