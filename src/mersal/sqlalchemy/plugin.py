from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mersal.lifespan import LifespanHandler
from mersal.logging import Logger
from mersal.plugins import Plugin
from mersal.sqlalchemy.sqlalchemy_poller import SQLAlchemyPoller, SQLAlchemyPollerConfig

if TYPE_CHECKING:
    from mersal.configuration import StandardConfigurator
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = (
    "SQLAlchemyPollerPlugin",
    "SQLAlchemyPollerPluginConfig",
)


@dataclass
class SQLAlchemyPollerPluginConfig:
    """Configuration for `SQLAlchemyPollerPlugin`."""

    async_session_factory: async_sessionmaker[AsyncSession]
    """Session factory used to create sessions for polling operations."""
    table_name: str
    """Polling results table name."""
    poll_interval: float = 0.1
    """Interval in seconds between poll checks when not using LISTEN/NOTIFY (default: 0.1)."""
    use_listen_notify: bool | None = None
    """Wake up `poll()` via Postgres LISTEN/NOTIFY instead of sleep-based polling.

    `None` (default) autodetects: enabled when the bound engine is PostgreSQL, and a
    plain sleep loop using `poll_interval` otherwise. Force `False` if you're behind a
    connection pooler in transaction-pooling mode (e.g. PgBouncer), where LISTEN/NOTIFY
    silently doesn't work because the backend connection can change between statements.
    Forcing `True` on a non-PostgreSQL engine raises during initialization.
    """
    listen_notify_fallback_interval: float = 5.0
    """When using LISTEN/NOTIFY, how often `poll()` re-checks the database even without
    a notification -- a safety net for a dropped or still-reconnecting LISTEN connection."""

    @property
    def plugin(self) -> SQLAlchemyPollerPlugin:
        return SQLAlchemyPollerPlugin(self)


class SQLAlchemyPollerPlugin(Plugin):
    """Owns a `SQLAlchemyPoller`'s lifecycle within a Mersal application.

    Builds the poller from `config`, using the app's registered `Logger` (so the
    poller and, on Postgres, its LISTEN/NOTIFY connection log through the same
    structured logger as the rest of the app), and wires it into the application
    lifespan: table creation (and, on Postgres, starting the LISTEN/NOTIFY
    connection) on startup, and `aclose` on shutdown. The poller is only built once
    the app resolves its `LifespanHandler`, and is exposed via the `poller` property
    from that point on so handlers can push and poll results.
    """

    poller: SQLAlchemyPoller
    """The poller, available once the app resolves its `LifespanHandler` (i.e. once
    this plugin's `__call__` has run as part of building a `Mersal` app)."""

    def __init__(self, config: SQLAlchemyPollerPluginConfig) -> None:
        self._config = config

    def __call__(self, configurator: StandardConfigurator) -> None:
        def decorate(configurator: StandardConfigurator) -> Any:
            lifespan_handler: LifespanHandler = configurator.get(LifespanHandler)  # type: ignore[type-abstract]
            logger: Logger = configurator.get(Logger)  # type: ignore[type-abstract]

            self.poller = SQLAlchemyPoller(
                SQLAlchemyPollerConfig(
                    async_session_factory=self._config.async_session_factory,
                    table_name=self._config.table_name,
                    poll_interval=self._config.poll_interval,
                    use_listen_notify=self._config.use_listen_notify,
                    listen_notify_fallback_interval=self._config.listen_notify_fallback_interval,
                    logger=logger,
                )
            )

            lifespan_handler.register_on_startup_hook(self.poller)
            lifespan_handler.register_on_shutdown_hook(self.poller.aclose)

            return lifespan_handler

        configurator.decorate(LifespanHandler, decorate)
