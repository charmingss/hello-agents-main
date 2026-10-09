from alembic.config import Config

from novel_agent.config import Settings


def configure_database_url(config: Config, settings: Settings | None = None) -> str:
    database_url = (settings or Settings()).database_url
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return database_url
