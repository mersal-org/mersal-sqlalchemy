from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mersal.lifespan import LifespanHandler
from mersal.logging import Logger
from mersal.plugins import Plugin
from mersal.sqlalchemy.sqlalchemy_poller import SQLAlchemyPoller, SQLAlchemyPollerConfig
from mersal.sqlalchemy.sqlalchemy_poller_with_cleanup import (
    SQLAlchemyPollerWithCleanup,
    SQLAlchemyPollerWithCleanupConfig,
)

if TYPE_CHECKING:
    from datetime import timedelta

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
    cleanup_interval: timedelta | None = None
    """How often to delete old polling results. Requires `cleanup_older_than` to also
    be set; when neither is set (the default), `poller` does no cleanup on its own."""
    cleanup_older_than: timedelta | None = None
    """Delete polling results older than this on each cleanup run. Requires
    `cleanup_interval` to also be set."""

    def __post_init__(self) -> None:
        if (self.cleanup_interval is None) != (self.cleanup_older_than is None):
            raise ValueError("cleanup_interval and cleanup_older_than must be set together.")

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

    When `config.cleanup_interval` and `config.cleanup_older_than` are both set,
    `poller` is a `SQLAlchemyPollerWithCleanup` wrapping the underlying
    `SQLAlchemyPoller` instead -- the lifespan hooks always target the underlying
    poller directly, since that's what owns the table/LISTEN connection.
    """

    poller: SQLAlchemyPoller | SQLAlchemyPollerWithCleanup
    """The poller, available once the app resolves its `LifespanHandler` (i.e. once
    this plugin's `__call__` has run as part of building a `Mersal` app)."""

    def __init__(self, config: SQLAlchemyPollerPluginConfig) -> None:
        self._config = config

    def __call__(self, configurator: StandardConfigurator) -> None:
        def decorate(configurator: StandardConfigurator) -> Any:
            lifespan_handler: LifespanHandler = configurator.get(LifespanHandler)  # type: ignore[type-abstract]
            logger: Logger = configurator.get(Logger)  # type: ignore[type-abstract]

            base_poller = SQLAlchemyPoller(
                SQLAlchemyPollerConfig(
                    async_session_factory=self._config.async_session_factory,
                    table_name=self._config.table_name,
                    poll_interval=self._config.poll_interval,
                    use_listen_notify=self._config.use_listen_notify,
                    listen_notify_fallback_interval=self._config.listen_notify_fallback_interval,
                    logger=logger,
                )
            )

            if self._config.cleanup_interval is not None and self._config.cleanup_older_than is not None:
                self.poller = SQLAlchemyPollerWithCleanup(
                    SQLAlchemyPollerWithCleanupConfig(
                        poller=base_poller,
                        cleanup_interval=self._config.cleanup_interval,
                        cleanup_older_than=self._config.cleanup_older_than,
                    )
                )
            else:
                self.poller = base_poller

            lifespan_handler.register_on_startup_hook(base_poller)
            lifespan_handler.register_on_shutdown_hook(base_poller.aclose)

            return lifespan_handler

        configurator.decorate(LifespanHandler, decorate)
