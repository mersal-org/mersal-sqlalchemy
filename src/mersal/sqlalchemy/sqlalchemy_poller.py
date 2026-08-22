from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, cast

from mersal.polling import Poller, PollingResult, ProblemDetails
from mersal.sqlalchemy.orm import create_polling_results_table, ensure_table_exists
from sqlalchemy import MergedResult, delete, insert, select, update
from sqlalchemy.orm import registry

if TYPE_CHECKING:
    from mersal.polling.poller import PollingStatus
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
    """Interval in seconds between poll checks (default: 0.1)."""

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
        while True:
            result = await self.peek(message_id, exclude_statuses=exclude_statuses)
            if result is not None:
                return result
            await asyncio.sleep(self._poll_interval)

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
