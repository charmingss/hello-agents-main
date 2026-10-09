import os
from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


def postgres_test_database_url() -> str:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError("external database tests require an explicit TEST_DATABASE_URL")
    if not database_url.startswith("postgresql+asyncpg://"):
        raise RuntimeError("integration tests require a postgresql+asyncpg database URL")
    return database_url


def require_external_database_enabled() -> None:
    if os.environ.get("RUN_EXTERNAL_TESTS") != "1":
        raise RuntimeError("external database tests require RUN_EXTERNAL_TESTS=1")
    postgres_test_database_url()


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    require_external_database_enabled()
    engine = create_async_engine(postgres_test_database_url(), pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            factory = async_sessionmaker(connection, expire_on_commit=False)
            async with factory() as session:
                yield session
            await transaction.rollback()
    finally:
        await engine.dispose()
