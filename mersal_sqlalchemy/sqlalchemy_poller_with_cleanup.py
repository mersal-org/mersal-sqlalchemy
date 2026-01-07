from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mersal_polling import PollingResult, ProblemDetails
    from mersal_polling.poller import PollingStatus

    from mersal_sqlalchemy.sqlalchemy_poller import SQLAlchemyPoller

__all__ = (
    "SQLAlchemyPollerWithCleanup",
    "SQLAlchemyPollerWithCleanupConfig",
)


@dataclass
class SQLAlchemyPollerWithCleanupConfig:
    """Configuration for SQLAlchemyPollerWithCleanup."""

    poller: SQLAlchemyPoller
    """The underlying SQLAlchemy poller."""
    cleanup_interval: timedelta
    """How often to run cleanup (default: 30 minutes)."""
    cleanup_older_than: timedelta
    """Delete results older than this (default: 30 minutes)."""

    @property
    def poller_with_cleanup(self) -> SQLAlchemyPollerWithCleanup:
        return SQLAlchemyPollerWithCleanup(self)


class SQLAlchemyPollerWithCleanup:
    """A poller that periodically cleans up old polling results.

    This wrapper composes a SQLAlchemyPoller with cleanup logic.
    After each poll operation, it checks if enough time has passed
    since the last cleanup and runs cleanup if needed.

    This is a temporary solution for serverless environments where
    cron jobs are not available.
    """

    def __init__(
        self,
        config: SQLAlchemyPollerWithCleanupConfig,
    ) -> None:
        self._poller = config.poller
        self._cleanup_interval = config.cleanup_interval
        self._cleanup_older_than = config.cleanup_older_than
        self._last_cleanup: datetime | None = None

    async def poll(
        self,
        message_id: Any,
        exclude_statuses: list[PollingStatus] | None = None,
    ) -> PollingResult:
        """Wait for and return the result of a message processing.

        After polling, checks if cleanup should run based on the cleanup interval.

        Args:
            message_id: The ID of the message to poll for
            exclude_statuses: Optional list of statuses to exclude from results.
                If the current status is in this list, poll will wait for an update.

        Returns:
            The polling result
        """
        result = await self._poller.poll(message_id, exclude_statuses=exclude_statuses)
        await self._maybe_cleanup()
        return result

    async def peek(
        self,
        message_id: Any,
        exclude_statuses: list[PollingStatus] | None = None,
    ) -> PollingResult | None:
        """Check if a result exists without blocking.

        This method returns immediately, either with a result or None.

        Args:
            message_id: The ID of the message to check
            exclude_statuses: Optional list of statuses to exclude from results.
                If the current status is in this list, None is returned.

        Returns:
            The polling result if available and not excluded, None otherwise
        """
        return await self._poller.peek(message_id, exclude_statuses=exclude_statuses)

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
        await self._poller.push(
            message_id,
            status=status,
            data=data,
            problem=problem,
        )

    async def _maybe_cleanup(self) -> None:
        """Run cleanup if enough time has passed since the last cleanup."""
        now = datetime.now(timezone.utc)

        if self._last_cleanup is None or (now - self._last_cleanup) >= self._cleanup_interval:
            await self._poller.cleanup(self._cleanup_older_than)
            self._last_cleanup = now
