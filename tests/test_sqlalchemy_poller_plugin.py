import uuid

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from mersal.configuration import StandardConfigurator
from mersal.lifespan import LifespanHandler
from mersal.lifespan.default_lifespan_handler import DefaultLifespanHandler
from mersal.logging import Logger, NullLogger
from mersal.sqlalchemy import SQLAlchemyPollerPluginConfig
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
        assert plugin.poller._logger is logger

        for hook in lifespan_handler.on_startup_hooks:
            await hook()

        async with db_engine.connect() as connection:
            table_exists = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).has_table(table_name)
            )
        assert table_exists

        message_id = uuid.uuid4()
        await plugin.poller.push(message_id, data={"ok": True})
        result = await plugin.poller.peek(message_id)
        assert result is not None
        assert result.data == {"ok": True}

        # The dedicated LISTEN connection the poller started is handed the same
        # logger, since it's what surfaces a dropped/reconnecting LISTEN connection.
        assert plugin.poller._listener is not None
        assert plugin.poller._listener._logger is logger

        for hook in lifespan_handler.on_shutdown_hooks:
            await hook()
