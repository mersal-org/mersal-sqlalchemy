from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING

from mersal.messages import MessageHeaders, TransportMessage
from mersal.sqlalchemy.orm import create_timeouts_table, prepare_table
from mersal.timeouts import DueMessage, TimeoutManager
from sqlalchemy import delete, insert, select

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

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
    schema: str | None = None
    """Schema the table lives in. Defaults to the connection's default schema (``search_path``)."""
    auto_create_table: bool = True
    """Create the table on startup if it doesn't exist.

    Set to False when tables are managed by migrations (e.g. with a separate migration role
    that owns the schema); startup then only checks that the table exists and raises
    `MissingTableError` if it doesn't. See `mersal.sqlalchemy.orm` for adding the table to
    your app's ``MetaData`` so Alembic autogenerate detects it.
    """

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
        self._schema = config.schema
        self._auto_create_table = config.auto_create_table
        self._table: Table | None = None

    async def __call__(self) -> None:
        table = create_timeouts_table(self._table_name, schema=self._schema)
        async with self._session_maker() as session:
            await session.run_sync(lambda s: prepare_table(table, s, auto_create=self._auto_create_table))
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
    async def get_due_messages(self) -> AsyncGenerator[Sequence[DueMessage]]:
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
