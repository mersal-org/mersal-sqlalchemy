import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from mersal.configuration import StandardConfigurator
from mersal.lifespan import LifespanHandler
from mersal.lifespan.default_lifespan_handler import DefaultLifespanHandler
from mersal.logging import Logger, NullLogger
from mersal.sqlalchemy import SQLAlchemyPoller, SQLAlchemyPollerPluginConfig, SQLAlchemyPollerWithCleanup
from mersal.testing.core.testing_utils import is_docker_available

__all__ = ("TestSQLAlchemyPollerPlugin",)


pytestmark = [
    pytest.mark.anyio,
    pytest.mark.usefixtures("postgres_service"),
    pytest.mark.skipif(not is_docker_available(), reason="docker not available on this platform"),
]


class _LoggerSpy(NullLogger):
    """A distinct `Logger` instance, so the test can assert on object identity to
    prove the app's registered logger (rather than a default) reached the poller."""


def _configurator_with_logger(logger: Logger) -> StandardConfigurator:
    configurator = StandardConfigurator()
    configurator.register(LifespanHandler, lambda _c: DefaultLifespanHandler())
    configurator.register(Logger, lambda _c: logger)
    return configurator


class TestSQLAlchemyPollerPlugin:
    async def test_wires_poller_into_lifespan_and_exposes_it(self, db_engine: AsyncEngine) -> None:
        async_session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
        table_name = f"polling_results_plugin_{uuid.uuid4().hex}"

        plugin = SQLAlchemyPollerPluginConfig(
            async_session_factory=async_session_factory,
            table_name=table_name,
        ).plugin

        logger = _LoggerSpy()
        configurator = _configurator_with_logger(logger)
        plugin(configurator)
        lifespan_handler = configurator.get(LifespanHandler)

        # The poller is only built once the app resolves its `LifespanHandler`, using
        # the `Logger` obtained from the dependency container.
        poller = plugin.poller
        assert isinstance(poller, SQLAlchemyPoller)
        assert poller._logger is logger

        for hook in lifespan_handler.on_startup_hooks:
            await hook()

        async with db_engine.connect() as connection:
            table_exists = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).has_table(table_name)
            )
        assert table_exists

        message_id = uuid.uuid4()
        await poller.push(message_id, data={"ok": True})
        result = await poller.peek(message_id)
        assert result is not None
        assert result.data == {"ok": True}

        # The dedicated LISTEN connection the poller started is handed the same
        # logger, since it's what surfaces a dropped/reconnecting LISTEN connection.
        assert poller._listener is not None
        assert poller._listener._logger is logger

        for hook in lifespan_handler.on_shutdown_hooks:
            await hook()

    async def test_cleanup_config_wraps_poller_with_cleanup(self, db_engine: AsyncEngine) -> None:
        async_session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
        table_name = f"polling_results_plugin_{uuid.uuid4().hex}"

        plugin = SQLAlchemyPollerPluginConfig(
            async_session_factory=async_session_factory,
            table_name=table_name,
            cleanup_interval=timedelta(seconds=0),
            cleanup_older_than=timedelta(seconds=0.05),
        ).plugin

        configurator = _configurator_with_logger(NullLogger())
        plugin(configurator)
        lifespan_handler = configurator.get(LifespanHandler)

        poller = plugin.poller
        assert isinstance(poller, SQLAlchemyPollerWithCleanup)

        for hook in lifespan_handler.on_startup_hooks:
            await hook()

        old_message_id = uuid.uuid4()
        await poller.push(old_message_id, data={"old": True})
        await asyncio.sleep(0.1)

        # `poll()` triggers cleanup as a side effect, so a fresh message pushed just
        # before it should still survive while the older one gets swept.
        new_message_id = uuid.uuid4()
        await poller.push(new_message_id, data={"new": True})
        result = await poller.poll(new_message_id)
        assert result.data == {"new": True}

        assert await poller.peek(old_message_id) is None

        for hook in lifespan_handler.on_shutdown_hooks:
            await hook()

    async def test_only_one_cleanup_field_set_raises(self) -> None:
        with pytest.raises(ValueError, match="cleanup_interval and cleanup_older_than must be set together"):
            SQLAlchemyPollerPluginConfig(
                async_session_factory=async_sessionmaker(),
                table_name="polling_results",
                cleanup_interval=timedelta(minutes=30),
            )
