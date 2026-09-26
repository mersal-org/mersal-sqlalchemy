import uuid
from collections.abc import AsyncGenerator, Callable
from typing import Any, cast

import pytest
from sqlalchemy import MetaData, Table, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from mersal.sqlalchemy import (
    SQLAlchemyOutboxStorageConfig,
    SQLAlchemyPollerConfig,
    SQLAlchemySagaStorageConfig,
    SQLAlchemyTimeoutManagerConfig,
)
from mersal.sqlalchemy.orm import (
    MissingTableError,
    create_outbox_table_and_map,
    create_polling_results_table,
    create_sagas_table,
    create_timeouts_table,
)
from mersal.testing.core.testing_utils import is_docker_available

__all__ = ("TestTableManagement",)


pytestmark = [
    pytest.mark.anyio,
    pytest.mark.usefixtures("postgres_service"),
    pytest.mark.skipif(not is_docker_available(), reason="docker not available on this platform"),
]


def _session_extractor(transaction_context: Any) -> AsyncSession:
    return cast("AsyncSession", transaction_context.items.get("sqlalchemy-session"))


def _saga_storage(factory: async_sessionmaker[AsyncSession], table_name: str, **kwargs: Any) -> Any:
    return SQLAlchemySagaStorageConfig(
        async_session_factory=factory, table_name=table_name, session_extractor=_session_extractor, **kwargs
    ).storage


def _outbox_storage(factory: async_sessionmaker[AsyncSession], table_name: str, **kwargs: Any) -> Any:
    return SQLAlchemyOutboxStorageConfig(
        async_session_factory=factory, table_name=table_name, session_extractor=_session_extractor, **kwargs
    ).storage


def _poller(factory: async_sessionmaker[AsyncSession], table_name: str, **kwargs: Any) -> Any:
    return SQLAlchemyPollerConfig(
        async_session_factory=factory, table_name=table_name, use_listen_notify=False, **kwargs
    ).poller


def _timeout_manager(factory: async_sessionmaker[AsyncSession], table_name: str, **kwargs: Any) -> Any:
    return SQLAlchemyTimeoutManagerConfig(async_session_factory=factory, table_name=table_name, **kwargs).storage


Component = Callable[..., Any]
TableBuilder = Callable[..., Table]

COMPONENTS = pytest.mark.parametrize(
    ("component", "table_builder"),
    [
        (_saga_storage, create_sagas_table),
        (_outbox_storage, create_outbox_table_and_map),
        (_poller, create_polling_results_table),
        (_timeout_manager, create_timeouts_table),
    ],
    ids=["sagas", "outbox", "poller", "timeouts"],
)


async def _table_names(engine: AsyncEngine, schema: str | None = None) -> list[str]:
    async with engine.connect() as conn:
        return await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names(schema=schema))


class TestTableManagement:
    @pytest.fixture
    def table_name(self) -> str:
        return f"mersal_{uuid.uuid4().hex}"

    @pytest.fixture
    def session_factory(self, db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
        return async_sessionmaker(db_engine, expire_on_commit=False)

    @pytest.fixture
    async def schema(self, db_engine: AsyncEngine) -> AsyncGenerator[str, None]:
        name = f"s_{uuid.uuid4().hex}"
        async with db_engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{name}"'))
        yield name
        async with db_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{name}" CASCADE'))

    @COMPONENTS
    async def test_raises_when_table_missing_and_auto_create_disabled(
        self,
        db_engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
        table_name: str,
        component: Component,
        table_builder: TableBuilder,
    ):
        subject = component(session_factory, table_name, auto_create_table=False)

        with pytest.raises(MissingTableError, match=table_name):
            await subject()

        assert table_name not in await _table_names(db_engine)

    @COMPONENTS
    async def test_uses_table_created_from_app_metadata(
        self,
        db_engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
        table_name: str,
        component: Component,
        table_builder: TableBuilder,
    ):
        app_metadata = MetaData()
        table_builder(table_name, app_metadata)
        async with db_engine.begin() as conn:
            await conn.run_sync(app_metadata.create_all)

        subject = component(session_factory, table_name, auto_create_table=False)
        await subject()

    @COMPONENTS
    async def test_creates_table_in_configured_schema(
        self,
        db_engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
        table_name: str,
        schema: str,
        component: Component,
        table_builder: TableBuilder,
    ):
        subject = component(session_factory, table_name, schema=schema)
        await subject()

        assert table_name in await _table_names(db_engine, schema=schema)
        assert table_name not in await _table_names(db_engine)

    @COMPONENTS
    async def test_table_builder_is_idempotent_per_metadata(
        self, table_name: str, component: Component, table_builder: TableBuilder
    ):
        app_metadata = MetaData()

        first = table_builder(table_name, app_metadata)
        second = table_builder(table_name, app_metadata)

        assert first is second
        assert list(app_metadata.tables) == [table_name]

    @COMPONENTS
    async def test_table_builder_respects_metadata_default_schema(
        self, table_name: str, component: Component, table_builder: TableBuilder
    ):
        app_metadata = MetaData(schema="tenant")

        tenant_table = table_builder(table_name, app_metadata)
        public_table = table_builder(table_name, app_metadata, schema="public")

        assert tenant_table.schema == "tenant"
        assert table_builder(table_name, app_metadata) is tenant_table
        assert public_table.schema == "public"
        assert table_builder(table_name, app_metadata, schema="public") is public_table


class TestRestrictedRole:
    """The migration-role pattern: an app role that can read/write data but not create tables."""

    @pytest.fixture
    async def restricted_engine(self, db_engine: AsyncEngine, db_config: dict) -> AsyncGenerator[AsyncEngine, None]:
        role = f"app_{uuid.uuid4().hex}"
        async with db_engine.begin() as conn:
            await conn.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD 'app'"))
            await conn.execute(text(f"GRANT pg_read_all_data, pg_write_all_data TO {role}"))
            await conn.execute(text(f"REVOKE CREATE ON SCHEMA public FROM {role}"))
        engine = create_async_engine(**{**db_config, "url": db_config["url"].set(username=role, password="app")})
        yield engine
        await engine.dispose()
        async with db_engine.begin() as conn:
            await conn.execute(text(f"DROP ROLE {role}"))

    async def test_role_cannot_create_tables(self, restricted_engine: AsyncEngine):
        subject = _timeout_manager(async_sessionmaker(restricted_engine), f"mersal_{uuid.uuid4().hex}")

        with pytest.raises(Exception, match="permission denied for schema public"):
            await subject()

    @COMPONENTS
    async def test_starts_with_table_created_by_migration_role(
        self,
        db_engine: AsyncEngine,
        restricted_engine: AsyncEngine,
        component: Component,
        table_builder: TableBuilder,
    ):
        table_name = f"mersal_{uuid.uuid4().hex}"
        app_metadata = MetaData()
        table_builder(table_name, app_metadata)
        async with db_engine.begin() as conn:
            await conn.run_sync(app_metadata.create_all)

        try:
            subject = component(
                async_sessionmaker(restricted_engine, expire_on_commit=False), table_name, auto_create_table=False
            )
            await subject()
        finally:
            async with db_engine.begin() as conn:
                await conn.run_sync(app_metadata.drop_all)
