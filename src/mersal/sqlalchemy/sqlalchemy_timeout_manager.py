from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING

from mersal.messages import MessageHeaders, TransportMessage
from mersal.sqlalchemy.orm import create_timeouts_table, ensure_table_exists
from mersal.timeouts import DueMessage, TimeoutManager
from sqlalchemy import delete, insert, select
from sqlalchemy.orm import registry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from sqlalchemy import Table
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = (
    "SQLAlchemyTimeoutManager",
    "SQLAlchemyTimeoutManagerConfig",
)


@dataclass
class SQLAlchemyTimeoutManagerConfig:
    """Configuration for SQLAlchemyTimeoutManager."""

    async_session_factory: async_sessionmaker[AsyncSession]
    "Session factory used for the timeouts table."
    table_name: str
    "Timeouts table name."
    batch_size: int = 100
    "Maximum number of due messages fetched per check."

    @property
    def storage(self) -> SQLAlchemyTimeoutManager:
        return SQLAlchemyTimeoutManager(self)


class SQLAlchemyTimeoutManager(TimeoutManager):
    """Stores deferred messages in a database table until they're due.

    Due messages are fetched with ``SELECT ... FOR UPDATE SKIP LOCKED`` inside a
    transaction that stays open while they're being sent, so several instances of the
    timeout manager app can share the table without sending a message twice: each
    one skips the rows another is sending. A sent message's row is deleted in that
    same transaction; if anything fails before it commits, the rows are left in place
    and are provided again on a later check.

    Headers are stored as JSON, since Mersal headers are always string-keyed and
    string-valued. The body is stored as bytes, so a serializer producing bytes is
    required.
    """

    def __init__(self, config: SQLAlchemyTimeoutManagerConfig) -> None:
        self._session_maker = config.async_session_factory
        self._table_name = config.table_name
        self._batch_size = config.batch_size
        self._table: Table | None = None

    async def __call__(self) -> None:
        table = create_timeouts_table(self._table_name, registry())
        async with self._session_maker() as session:
            await session.run_sync(lambda s: ensure_table_exists(table, s))
            await session.commit()
        self._table = table

    async def defer(self, due_time: datetime, message: TransportMessage) -> None:
        async with self._session_maker() as session:
            await session.execute(
                insert(self.table).values(
                    due_time=due_time,
                    headers=dict(message.headers),
                    body=message.body,
                )
            )
            await session.commit()

    @asynccontextmanager
    async def get_due_messages(self) -> AsyncIterator[Sequence[DueMessage]]:
        table = self.table
        async with self._session_maker() as session, session.begin():
            stmt = (
                select(table.c.id, table.c.headers, table.c.body)
                .where(table.c.due_time <= datetime.now(UTC))
                .order_by(table.c.due_time, table.c.id)
                .limit(self._batch_size)
                .with_for_update(skip_locked=True)
            )
            rows = (await session.execute(stmt)).all()
            yield [
                DueMessage(
                    TransportMessage(row.body, MessageHeaders(row.headers)),
                    partial(self._delete, session, row.id),
                )
                for row in rows
            ]

    @property
    def table(self) -> Table:
        if self._table is None:
            raise RuntimeError("SQLAlchemyTimeoutManager is not initialized; it's initialized on app startup")
        return self._table

    async def _delete(self, session: AsyncSession, _id: int) -> None:
        await session.execute(delete(self.table).where(self.table.c.id == _id))
