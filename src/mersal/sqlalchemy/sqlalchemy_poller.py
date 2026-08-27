from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, cast

import anyio

from mersal.logging import NullLogger
from mersal.polling import Poller, PollingResult, ProblemDetails
from mersal.sqlalchemy.orm import create_polling_results_table, ensure_table_exists
from mersal.sqlalchemy.postgres_notify_listener import PostgresNotifyListener
from sqlalchemy import MergedResult, delete, func, insert, select, update
from sqlalchemy.orm import registry

if TYPE_CHECKING:
    from mersal.logging import Logger
    from mersal.polling.poller import PollingStatus
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

__all__ = (
    "SQLAlchemyPoller",
    "SQLAlchemyPollerConfig",
)


@dataclass
class SQLAlchemyPollerConfig:
    """Configuration for SQLAlchemyPoller."""

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
    logger: Logger | None = None
    """Structured logger used by the poller and, when LISTEN/NOTIFY is enabled, the
    underlying `PostgresNotifyListener`. Defaults to a no-op logger."""

    @property
    def poller(self) -> SQLAlchemyPoller:
        return SQLAlchemyPoller(self)


class SQLAlchemyPoller(Poller):
    def __init__(
        self,
        config: SQLAlchemyPollerConfig,
    ) -> None:
        self._session_maker = config.async_session_factory
        self._table_name = config.table_name
        self._poll_interval = config.poll_interval
        self._use_listen_notify_config = config.use_listen_notify
        self._notify_fallback_interval = config.listen_notify_fallback_interval
        self._logger = config.logger or NullLogger()
        self._listener: PostgresNotifyListener | None = None

    async def poll(
        self,
        message_id: Any,
        exclude_statuses: list[PollingStatus] | None = None,
    ) -> PollingResult:
        """Wait for and return the result of a message processing.

        This method blocks until the result is available and matches the filter criteria.

        Args:
            message_id: The ID of the message to poll for
            exclude_statuses: Optional list of statuses to exclude from results.
                If the current status is in this list, poll will wait for an update.

        Returns:
            The polling result
        """
        if self._listener is None:
            while True:
                result = await self.peek(message_id, exclude_statuses=exclude_statuses)
                if result is not None:
                    return result
                await asyncio.sleep(self._poll_interval)

        while True:
            async with self._listener.subscribe(str(message_id)) as event:
                result = await self.peek(message_id, exclude_statuses=exclude_statuses)
                if result is not None:
                    return result
                with anyio.move_on_after(self._notify_fallback_interval):
                    await event.wait()

    async def peek(
        self,
        message_id: Any,
        exclude_statuses: list[PollingStatus] | None = None,
    ) -> PollingResult | None:
        """Check if a result exists without blocking.

        This method returns immediately, either with a result or None.
        Useful for client-side polling scenarios.

        Args:
            message_id: The ID of the message to check
            exclude_statuses: Optional list of statuses to exclude from results.
                If the current status is in this list, None is returned.

        Returns:
            The polling result if available and not excluded, None otherwise
        """
        async with self._session_maker() as session:
            stmt = select(self.table).where(self.table.c.message_id == str(message_id))
            result = (await session.execute(stmt)).first()

            if result is None:
                return None

            # Check if the status should be excluded
            if exclude_statuses is not None and result.status in exclude_statuses:
                return None

            problem = None
            if result.problem is not None:
                problem = ProblemDetails(**result.problem)

            return PollingResult(
                message_id=message_id,
                status=result.status,
                data=result.data,
                problem=problem,
            )

    async def push(
        self,
        message_id: Any,
        status: PollingStatus = "succeeded",
        data: dict[str, Any] | None = None,
        problem: ProblemDetails | None = None,
    ) -> None:
        """Store the result of a message processing.

        Args:
            message_id: The ID of the message
            status: The status of the operation (accepted, succeeded, failed)
            data: Success data (for rich results, batch operations)
            problem: Structured error information (RFC 7807) for failures
        """
        async with self._session_maker() as session:
            problem_dict = None
            if problem is not None:
                problem_dict = {
                    "type": problem.type,
                    "title": problem.title,
                    "status": problem.status,
                    "detail": problem.detail,
                    "instance": problem.instance,
                    "extensions": problem.extensions,
                }

            # Check if a record already exists
            stmt = select(self.table).where(self.table.c.message_id == str(message_id))
            existing = (await session.execute(stmt)).first()

            if existing is not None:
                # Update existing record to allow status transitions
                update_stmt = (
                    update(self.table)
                    .where(self.table.c.message_id == str(message_id))
                    .values(
                        status=status,
                        data=data,
                        problem=problem_dict,
                    )
                )
                await session.execute(update_stmt)
            else:
                # Insert new record
                await session.execute(
                    insert(self.table),
                    [
                        {
                            "message_id": str(message_id),
                            "status": status,
                            "data": data,
                            "problem": problem_dict,
                            "created_at": datetime.now(timezone.utc),
                        }
                    ],
                )

            if self._listener is not None:
                # Sent inside this same transaction: Postgres only actually delivers a
                # NOTIFY if the transaction that issued it commits, so this can never
                # wake a waiter for a write that gets rolled back.
                await session.execute(select(func.pg_notify(self._listener.channel, str(message_id))))

            await session.commit()

    async def cleanup(self, older_than: timedelta) -> int:
        """Clean up old polling results.

        Args:
            older_than: Delete results older than this timedelta

        Returns:
            Number of records deleted
        """
        cutoff_time = datetime.now(timezone.utc) - older_than
        async with self._session_maker() as session:
            stmt = delete(self.table).where(self.table.c.created_at < cutoff_time)
            result = cast(MergedResult, await session.execute(stmt))
            await session.commit()
            return cast(int, result.rowcount)

    async def __call__(self) -> None:
        """Initialize the poller by creating the table if needed."""
        self.table = create_polling_results_table(self._table_name, registry())
        async with self._session_maker() as session:
            await session.run_sync(lambda s: ensure_table_exists(self.table, s))
            await session.commit()
            engine = session.bind

        is_postgres = engine is not None and engine.dialect.name == "postgresql"
        if self._use_listen_notify_config and not is_postgres:
            raise ValueError("use_listen_notify=True requires a PostgreSQL engine.")

        use_listen_notify = is_postgres if self._use_listen_notify_config is None else self._use_listen_notify_config
        if use_listen_notify and self._listener is None:
            self._listener = PostgresNotifyListener(
                cast("AsyncEngine", engine),
                channel=PostgresNotifyListener.channel_for(self._table_name),
                logger=self._logger,
            )
            self._listener.start()

    async def aclose(self) -> None:
        """Release resources held by the poller, such as a dedicated LISTEN connection."""
        if self._listener is not None:
            await self._listener.aclose()
