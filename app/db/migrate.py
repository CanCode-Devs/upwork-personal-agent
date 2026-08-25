from pathlib import Path

from alembic import command
from alembic.config import Config

from app.config import get_settings


def run_migrations() -> None:
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    sqlite_prefix = "sqlite:///"
    if settings.database_url.startswith(sqlite_prefix):
        Path(settings.database_url.removeprefix(sqlite_prefix)).parent.mkdir(parents=True, exist_ok=True)
    config = Config(str(Path("alembic.ini")))
    command.upgrade(config, "head")
