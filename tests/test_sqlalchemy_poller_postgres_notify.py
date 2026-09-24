import uuid
from contextlib import asynccontextmanager
from time import monotonic

import anyio
import pytest
from pytest_databases.docker.postgres import PostgresService
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from mersal.sqlalchemy import SQLAlchemyPoller, SQLAlchemyPollerConfig

__all__ = (
    "TestSQLAlchemyPollerPostgresNotify",
    "anyio_backend",
    "postgres_engine",
)


pytest_plugins = ["pytest_databases.docker.postgres"]

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    # asyncpg only runs on asyncio, not trio.
    return "asyncio"


@pytest.fixture
async def postgres_engine(postgres_18_service: PostgresService):
    # Named postgres_18_service rather than the plugin's generic postgres_service:
    # tests/conftest.py already defines its own (unrelated, value-less) postgres_service
    # fixture for the docker-compose-based tests elsewhere in this suite, and a local
    # conftest.py fixture shadows a same-named one from a plugin.
    engine = create_async_engine(
        URL.create(
            drivername="postgresql+asyncpg",
            username=postgres_18_service.user,
            password=postgres_18_service.password,
            host=postgres_18_service.host,
            port=postgres_18_service.port,
            database=postgres_18_service.database,
        )
    )
    try:
        yield engine
    finally:
        await engine.dispose()


@asynccontextmanager
async def _running_poller(config: SQLAlchemyPollerConfig):
    poller = SQLAlchemyPoller(config)
    await poller()
    try:
        yield poller
    finally:
        await poller.aclose()


class TestSQLAlchemyPollerPostgresNotify:
    async def test_uses_listen_notify_by_default(self, postgres_engine: AsyncEngine):
        config = SQLAlchemyPollerConfig(
            table_name=f"polling_results_{uuid.uuid4().hex[:8]}",
            async_session_factory=async_sessionmaker(postgres_engine, expire_on_commit=False),
        )

        async with _running_poller(config) as subject:
            assert subject._listener is not None

    async def test_poll_wakes_up_via_notify_faster_than_fallback_interval(self, postgres_engine: AsyncEngine):
        config = SQLAlchemyPollerConfig(
            table_name=f"polling_results_{uuid.uuid4().hex[:8]}",
            async_session_factory=async_sessionmaker(postgres_engine, expire_on_commit=False),
            # A fallback interval this long proves nothing but LISTEN/NOTIFY could have
            # woken poll() up in time -- a plain sleep loop would have blocked for the
            # whole interval instead.
            listen_notify_fallback_interval=30,
        )

        async with _running_poller(config) as subject, anyio.create_task_group() as tg:
            message_id = uuid.uuid4()
            data = {"result": "fast"}

            async def push_after_delay():
                await anyio.sleep(0.3)
                await subject.push(message_id, data=data)

            tg.start_soon(push_after_delay)

            started_at = monotonic()
            result = await subject.poll(message_id)
            elapsed = monotonic() - started_at

            assert result.data == data
            assert elapsed < 5

    async def test_use_listen_notify_false_falls_back_to_sleep_polling(self, postgres_engine: AsyncEngine):
        config = SQLAlchemyPollerConfig(
            table_name=f"polling_results_{uuid.uuid4().hex[:8]}",
            async_session_factory=async_sessionmaker(postgres_engine, expire_on_commit=False),
            use_listen_notify=False,
            poll_interval=0.05,
        )

        async with _running_poller(config) as subject, anyio.create_task_group() as tg:
            assert subject._listener is None

            message_id = uuid.uuid4()
            data = {"result": "delayed"}

            async def push_after_delay():
                await anyio.sleep(0.2)
                await subject.push(message_id, data=data)

            tg.start_soon(push_after_delay)

            result = await subject.poll(message_id)

            assert result.data == data

    async def test_use_listen_notify_true_requires_postgres_engine(self, postgres_engine: AsyncEngine):
        config = SQLAlchemyPollerConfig(
            table_name=f"polling_results_{uuid.uuid4().hex[:8]}",
            async_session_factory=async_sessionmaker(postgres_engine, expire_on_commit=False),
            use_listen_notify=True,
        )

        # postgres_engine is Postgres, so this should succeed rather than raise.
        async with _running_poller(config) as subject:
            assert subject._listener is not None

    async def test_listen_false_still_notifies_listening_pollers(self, postgres_engine: AsyncEngine):
        table_name = f"polling_results_{uuid.uuid4().hex[:8]}"
        session_factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
        listening_config = SQLAlchemyPollerConfig(
            table_name=table_name,
            async_session_factory=session_factory,
            listen_notify_fallback_interval=30,
        )
        pushing_config = SQLAlchemyPollerConfig(
            table_name=table_name,
            async_session_factory=session_factory,
            listen=False,
        )

        async with (
            _running_poller(listening_config) as listening,
            _running_poller(pushing_config) as pushing,
            anyio.create_task_group() as tg,
        ):
            assert pushing._listener is None

            message_id = uuid.uuid4()
            data = {"result": "from another process"}

            async def push_after_delay():
                await anyio.sleep(0.3)
                await pushing.push(message_id, data=data)

            tg.start_soon(push_after_delay)

            started_at = monotonic()
            result = await listening.poll(message_id)
            elapsed = monotonic() - started_at

            assert result.data == data
            assert elapsed < 5

    async def test_listens_on_listen_engine_when_given(self, postgres_engine: AsyncEngine):
        listen_engine = create_async_engine(postgres_engine.url)
        config = SQLAlchemyPollerConfig(
            table_name=f"polling_results_{uuid.uuid4().hex[:8]}",
            async_session_factory=async_sessionmaker(postgres_engine, expire_on_commit=False),
            listen_engine=listen_engine,
            listen_notify_fallback_interval=30,
        )

        try:
            async with _running_poller(config) as subject, anyio.create_task_group() as tg:
                assert subject._listener is not None
                assert subject._listener._engine is listen_engine

                message_id = uuid.uuid4()

                async def push_after_delay():
                    await anyio.sleep(0.3)
                    await subject.push(message_id)

                tg.start_soon(push_after_delay)

                started_at = monotonic()
                await subject.poll(message_id)

                assert monotonic() - started_at < 5
        finally:
            await listen_engine.dispose()
