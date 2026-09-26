import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from mersal.activation import BuiltinHandlerActivator
from mersal.core.app import Mersal
from mersal.messages import MessageHeaders, TransportMessage
from mersal.pipeline import MessageContext
from mersal.serialization.identity_serializer import IdentitySerializer
from mersal.sqlalchemy import SQLAlchemyTimeoutManager, SQLAlchemyTimeoutManagerConfig
from mersal.testing.core.testing_utils import is_docker_available
from mersal.timeouts import TimeoutsConfig
from mersal.transport.in_memory import InMemoryNetwork
from mersal.transport.in_memory.in_memory_transport_plugin import InMemoryTransportPluginConfig

__all__ = ("TestSQLAlchemyTimeoutManager",)


pytestmark = [
    pytest.mark.anyio,
    pytest.mark.usefixtures("postgres_service"),
    pytest.mark.skipif(not is_docker_available(), reason="docker not available on this platform"),
]


def _message(body: bytes = b"body", **headers: str) -> TransportMessage:
    return TransportMessage(body, MessageHeaders({"message_id": str(uuid.uuid4()), **headers}))


async def _row_count(db_engine: AsyncEngine, subject: SQLAlchemyTimeoutManager) -> int:
    async with db_engine.connect() as conn:
        return (await conn.execute(select(func.count()).select_from(subject.table))).scalar_one()


class TestSQLAlchemyTimeoutManager:
    @pytest.fixture
    def table_name(self) -> str:
        return f"timeouts_{uuid.uuid4().hex}"

    @pytest.fixture
    async def subject(self, db_engine: AsyncEngine, table_name: str) -> SQLAlchemyTimeoutManager:
        subject = SQLAlchemyTimeoutManagerConfig(
            async_session_factory=async_sessionmaker(db_engine, expire_on_commit=False),
            table_name=table_name,
        ).storage
        await subject()
        return subject

    async def test_creates_table_idempotently(
        self, db_engine: AsyncEngine, subject: SQLAlchemyTimeoutManager, table_name: str
    ):
        await subject()

        async with db_engine.connect() as conn:
            tables = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
        assert table_name in tables

    async def test_provides_only_due_messages_ordered_by_due_time(self, subject: SQLAlchemyTimeoutManager):
        now = datetime.now(UTC)
        later = _message(b"later", custom="header")
        earlier = _message(b"earlier")
        await subject.defer(now - timedelta(seconds=1), later)
        await subject.defer(now - timedelta(seconds=2), earlier)
        await subject.defer(now + timedelta(minutes=1), _message(b"future"))

        async with subject.get_due_messages() as due_messages:
            messages = [x.message for x in due_messages]

        assert [m.body for m in messages] == [b"earlier", b"later"]
        assert messages[1].headers == later.headers
        assert isinstance(messages[1].headers, MessageHeaders)

    async def test_only_completed_messages_are_removed(self, subject: SQLAlchemyTimeoutManager):
        past = datetime.now(UTC) - timedelta(seconds=1)
        await subject.defer(past, _message(b"first"))
        await subject.defer(past + timedelta(milliseconds=1), _message(b"second"))

        async with subject.get_due_messages() as due_messages:
            await due_messages[0].mark_as_completed()

        async with subject.get_due_messages() as due_messages:
            assert [x.message.body for x in due_messages] == [b"second"]

    async def test_nothing_is_removed_when_sending_fails(self, subject: SQLAlchemyTimeoutManager):
        await subject.defer(datetime.now(UTC) - timedelta(seconds=1), _message())

        with pytest.raises(RuntimeError):
            async with subject.get_due_messages() as due_messages:
                await due_messages[0].mark_as_completed()
                raise RuntimeError("send failed")

        async with subject.get_due_messages() as due_messages:
            assert len(due_messages) == 1

    async def test_concurrent_checks_do_not_get_the_same_messages(self, subject: SQLAlchemyTimeoutManager):
        past = datetime.now(UTC) - timedelta(seconds=1)
        await subject.defer(past, _message(b"first"))
        await subject.defer(past + timedelta(milliseconds=1), _message(b"second"))

        async with subject.get_due_messages() as first_batch, subject.get_due_messages() as second_batch:
            assert len(first_batch) == 2
            assert second_batch == []

    async def test_fetches_at_most_batch_size_messages(self, db_engine: AsyncEngine, table_name: str):
        subject = SQLAlchemyTimeoutManagerConfig(
            async_session_factory=async_sessionmaker(db_engine, expire_on_commit=False),
            table_name=table_name,
            batch_size=2,
        ).storage
        await subject()
        past = datetime.now(UTC) - timedelta(seconds=1)
        for _ in range(3):
            await subject.defer(past, _message())

        async with subject.get_due_messages() as due_messages:
            assert len(due_messages) == 2

    async def test_app_handles_deferred_message_once_due(
        self, db_engine: AsyncEngine, subject: SQLAlchemyTimeoutManager
    ):
        network = InMemoryNetwork()
        activator = BuiltinHandlerActivator()
        received: list[tuple[bytes, MessageHeaders]] = []

        def handler_factory(message_context: MessageContext, _: Mersal) -> Any:
            async def handler(message: bytes) -> None:
                received.append((message, message_context.headers))

            return handler

        activator.register(bytes, handler_factory)
        app = Mersal(
            "m",
            activator,
            plugins=[InMemoryTransportPluginConfig(network, "test-queue").plugin],
            serializer=IdentitySerializer(),
            timeouts=TimeoutsConfig(storage=subject, poll_interval=0.1),
        )

        await app.start()
        try:
            await app.defer_local(timedelta(milliseconds=500), b"later")
            with anyio.fail_after(3):
                while not await _row_count(db_engine, subject):
                    await anyio.sleep(0.05)
            assert received == []
            with anyio.fail_after(5):
                while not received:
                    await anyio.sleep(0.05)
        finally:
            await app.stop()

        body, headers = received[0]
        assert body == b"later"
        assert MessageHeaders.deferred_until_key not in headers
        with anyio.fail_after(3):
            while await _row_count(db_engine, subject):
                await anyio.sleep(0.05)
