from __future__ import annotations

import asyncio
import contextlib
import hashlib
from typing import TYPE_CHECKING, Any

import anyio

from mersal.logging import NullLogger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mersal.logging import Logger
    from sqlalchemy.ext.asyncio import AsyncEngine

__all__ = ("PostgresNotifyListener",)

# Postgres stores LISTEN/NOTIFY channel names as a `Name` type and silently truncates
# anything past NAMEDATALEN - 1 bytes (63 by default) instead of raising -- so two
# distinct, long table names that only differ after that point would otherwise
# collide on the same channel.
_MAX_CHANNEL_NAME_LENGTH = 63


class PostgresNotifyListener:
    """Wakes up waiting pollers via Postgres LISTEN/NOTIFY instead of sleep-based polling.

    Holds one dedicated connection open on ``engine`` for as long as the listener runs
    and LISTENs on ``channel``. ``SQLAlchemyPoller.push`` sends a NOTIFY carrying the
    message id as payload (via ``pg_notify()``, in the same transaction as the result
    row) so this listener only ever fans out notifications for writes that actually
    committed. Whoever is waiting in ``subscribe()`` for that message id gets woken up
    immediately instead of on the next sleep tick.

    This requires a connection that isn't swapped out mid-session: PgBouncer's
    transaction pooling mode breaks LISTEN/NOTIFY because the backend connection can
    change between statements. Pass ``use_listen_notify=False`` in
    ``SQLAlchemyPollerConfig`` if you're behind one of those.

    Correctness never depends on this connection being up: if it's not yet connected,
    or it drops and is reconnecting, waiting on ``subscribe()``'s event just times out
    and the caller falls back to re-checking the database directly.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        channel: str,
        reconnect_backoff: float = 1.0,
        max_reconnect_backoff: float = 30.0,
        logger: Logger | None = None,
    ) -> None:
        self._engine = engine
        self.channel = channel
        self._reconnect_backoff = reconnect_backoff
        self._max_reconnect_backoff = max_reconnect_backoff
        self._logger = logger or NullLogger()
        self._waiters: dict[str, list[anyio.Event]] = {}
        self._task: asyncio.Task[None] | None = None

    @staticmethod
    def channel_for(table_name: str) -> str:
        """Derive a NOTIFY channel name for ``table_name`` within Postgres's 63-byte limit.

        Always appends a short hash of the full table name rather than only doing so
        once truncation kicks in, so the mapping from table name to channel is a single
        code path and two table names that share a long prefix can never collide.
        """
        prefix = "mersal_poll_"
        suffix = hashlib.sha256(table_name.encode()).hexdigest()[:8]
        budget = _MAX_CHANNEL_NAME_LENGTH - len(prefix) - len(suffix) - 1
        return f"{prefix}{table_name[:budget]}_{suffix}"

    def start(self) -> None:
        """Start the background LISTEN connection."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run())

    async def aclose(self) -> None:
        """Stop listening and release the dedicated connection."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    @contextlib.asynccontextmanager
    async def subscribe(self, message_id: str) -> AsyncIterator[anyio.Event]:
        """Register interest in ``message_id`` for the duration of the ``with`` block.

        Register the waiter *before* checking the database for a result, not after --
        otherwise a notification that arrives while that check is in flight would have
        nowhere to land and get dropped, silently falling back to the full poll
        interval. Registering first means that race instead resolves in our favor: the
        yielded event is already set by the time we get around to waiting on it.
        """
        event = anyio.Event()
        self._waiters.setdefault(message_id, []).append(event)
        try:
            yield event
        finally:
            waiters = self._waiters.get(message_id)
            if waiters is not None:
                waiters.remove(event)
                if not waiters:
                    del self._waiters[message_id]

    def _on_notification(self, _connection: Any, _pid: int, channel: str, payload: str) -> None:
        if channel != self.channel:
            return
        for event in self._waiters.get(payload, ()):
            event.set()

    async def _run(self) -> None:
        # Ended by `aclose()` cancelling this task, which surfaces as CancelledError
        # at whichever await point is active and propagates straight out below.
        backoff = self._reconnect_backoff
        while True:
            connected_at = anyio.current_time()
            try:
                await self._listen_until_disconnected()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception("mersal_sqlalchemy.postgres_notify_listener.listen_failed", channel=self.channel)

            # A connection that stayed up a good while is treated as a fresh start:
            # retry quickly rather than carrying over a long backoff from an old,
            # unrelated outage.
            if anyio.current_time() - connected_at > backoff * 4:
                backoff = self._reconnect_backoff
            else:
                backoff = min(backoff * 2, self._max_reconnect_backoff)
            await asyncio.sleep(backoff)

    async def _listen_until_disconnected(self) -> None:
        async with self._engine.connect() as connection:
            raw_connection = await connection.get_raw_connection()
            asyncpg_connection = raw_connection.driver_connection
            if asyncpg_connection is None:
                raise RuntimeError("Postgres connection has no underlying driver connection.")

            terminated = anyio.Event()
            asyncpg_connection.add_termination_listener(lambda _conn: terminated.set())
            await asyncpg_connection.add_listener(self.channel, self._on_notification)
            try:
                await terminated.wait()
            finally:
                with contextlib.suppress(Exception):
                    await asyncpg_connection.remove_listener(self.channel, self._on_notification)
