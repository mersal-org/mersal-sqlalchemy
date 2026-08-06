import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from mersal.polling import ProblemDetails
from mersal.sqlalchemy import (
    SQLAlchemyPoller,
    SQLAlchemyPollerConfig,
    SQLAlchemyPollerWithCleanup,
    SQLAlchemyPollerWithCleanupConfig,
)
from mersal.testing.core.testing_utils import is_docker_available

__all__ = ("TestSQLAlchemyPoller",)


pytestmark = [
    pytest.mark.anyio,
    pytest.mark.usefixtures("postgres_service"),
    pytest.mark.skipif(not is_docker_available(), reason="docker not available on this platform"),
]


class TestSQLAlchemyPoller:
    async def test_creates_table(self, db_engine: AsyncEngine):
        table_name = f"polling_results_{uuid.uuid4().hex[:8]}"
        async_session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
        config = SQLAlchemyPollerConfig(
            table_name=table_name,
            async_session_factory=async_session_factory,
        )

        subject = SQLAlchemyPoller(config)
        await subject()

        async with db_engine.connect() as conn:
            tables = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())

        assert table_name in tables

    async def test_with_created_table(self, db_engine: AsyncEngine):
        table_name = f"polling_results_{uuid.uuid4().hex[:8]}"
        async_session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
        config = SQLAlchemyPollerConfig(
            table_name=table_name,
            async_session_factory=async_session_factory,
        )

        subject = SQLAlchemyPoller(config)
        await subject()
        await subject()

        async with db_engine.connect() as conn:
            tables = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())

        assert table_name in tables

    async def test_push_and_peek_success(self, db_engine: AsyncEngine):
        async_session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
        config = SQLAlchemyPollerConfig(
            table_name=f"polling_results_{uuid.uuid4().hex[:8]}",
            async_session_factory=async_session_factory,
        )

        subject = SQLAlchemyPoller(config)
        await subject()

        message_id = uuid.uuid4()
        data = {"result": "success", "value": 42}

        await subject.push(message_id, data=data)
        result = await subject.peek(message_id)

        assert result is not None
        assert result.message_id == message_id
        assert result.data == data
        assert result.problem is None
        assert result.is_success

    async def test_push_and_peek_failure(self, db_engine: AsyncEngine):
        async_session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
        config = SQLAlchemyPollerConfig(
            table_name=f"polling_results_{uuid.uuid4().hex[:8]}",
            async_session_factory=async_session_factory,
        )

        subject = SQLAlchemyPoller(config)
        await subject()

        message_id = uuid.uuid4()
        problem = ProblemDetails(
            type="https://example.com/errors/validation",
            title="Validation Error",
            status=400,
            detail="Invalid input data",
            instance="/api/messages/123",
            extensions={"field": "email"},
        )

        await subject.push(message_id, problem=problem)
        result = await subject.peek(message_id)

        assert result is not None
        assert result.message_id == message_id
        assert result.data is None
        assert result.problem is not None
        assert result.problem.type == problem.type
        assert result.problem.title == problem.title
        assert result.problem.status == problem.status
        assert result.problem.detail == problem.detail
        assert result.problem.instance == problem.instance
        assert result.problem.extensions == problem.extensions
        assert result.is_failure

    async def test_peek_nonexistent(self, db_engine: AsyncEngine):
        async_session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
        config = SQLAlchemyPollerConfig(
            table_name=f"polling_results_{uuid.uuid4().hex[:8]}",
            async_session_factory=async_session_factory,
        )

        subject = SQLAlchemyPoller(config)
        await subject()

        message_id = uuid.uuid4()
        result = await subject.peek(message_id)

        assert result is None

    async def test_poll_waits_for_result(self, db_engine: AsyncEngine):
        async_session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
        config = SQLAlchemyPollerConfig(
            table_name=f"polling_results_{uuid.uuid4().hex[:8]}",
            async_session_factory=async_session_factory,
            poll_interval=0.05,
        )

        subject = SQLAlchemyPoller(config)
        await subject()

        message_id = uuid.uuid4()
        data = {"result": "delayed"}

        async def push_after_delay():
            await asyncio.sleep(0.2)
            await subject.push(message_id, data=data)

        asyncio.create_task(push_after_delay())

        result = await subject.poll(message_id)

        assert result.message_id == message_id
        assert result.data == data
        assert result.is_success

    async def test_cleanup_removes_old_results(self, db_engine: AsyncEngine):
        async_session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
        config = SQLAlchemyPollerConfig(
            table_name=f"polling_results_{uuid.uuid4().hex[:8]}",
            async_session_factory=async_session_factory,
        )

        subject = SQLAlchemyPoller(config)
        await subject()

        message_id_1 = uuid.uuid4()
        message_id_2 = uuid.uuid4()

        await subject.push(message_id_1, data={"test": 1})
        await subject.push(message_id_2, data={"test": 2})

        async with async_session_factory() as session:
            count_before = len((await session.execute(select(subject.table))).all())
        assert count_before == 2

        deleted_count = await subject.cleanup(older_than=timedelta(seconds=-1))
        assert deleted_count == 2

        async with async_session_factory() as session:
            count_after = len((await session.execute(select(subject.table))).all())
        assert count_after == 0

    async def test_cleanup_preserves_recent_results(self, db_engine: AsyncEngine):
        async_session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
        config = SQLAlchemyPollerConfig(
            table_name=f"polling_results_{uuid.uuid4().hex[:8]}",
            async_session_factory=async_session_factory,
        )

        subject = SQLAlchemyPoller(config)
        await subject()

        message_id = uuid.uuid4()
        await subject.push(message_id, data={"test": 1})

        deleted_count = await subject.cleanup(older_than=timedelta(hours=1))
        assert deleted_count == 0

        result = await subject.peek(message_id)
        assert result is not None


class TestSQLAlchemyPollerWithCleanup:
    async def test_poll_triggers_cleanup(self, db_engine: AsyncEngine):
        async_session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
        poller_config = SQLAlchemyPollerConfig(
            table_name=f"polling_results_{uuid.uuid4().hex[:8]}",
            async_session_factory=async_session_factory,
            poll_interval=0.05,
        )

        base_poller = SQLAlchemyPoller(poller_config)
        await base_poller()

        cleanup_config = SQLAlchemyPollerWithCleanupConfig(
            poller=base_poller,
            cleanup_interval=timedelta(seconds=0.1),
            cleanup_older_than=timedelta(seconds=0.15),
        )

        subject = SQLAlchemyPollerWithCleanup(cleanup_config)

        old_message_id = uuid.uuid4()
        await subject.push(old_message_id, data={"old": True})

        await asyncio.sleep(0.2)

        async with async_session_factory() as session:
            count_before = len((await session.execute(select(base_poller.table))).all())
        assert count_before == 1

        new_message_id = uuid.uuid4()

        async def push_after_delay():
            await asyncio.sleep(0.1)
            await subject.push(new_message_id, data={"new": True})

        task = asyncio.create_task(push_after_delay())

        result = await subject.poll(new_message_id)
        await task

        assert result.message_id == new_message_id
        assert result.data == {"new": True}

        async with async_session_factory() as session:
            results = (await session.execute(select(base_poller.table))).all()
            count_after = len(results)
        assert count_after == 1
        assert results[0].message_id == str(new_message_id)

    async def test_peek_does_not_trigger_cleanup(self, db_engine: AsyncEngine):
        async_session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
        poller_config = SQLAlchemyPollerConfig(
            table_name=f"polling_results_{uuid.uuid4().hex[:8]}",
            async_session_factory=async_session_factory,
        )

        base_poller = SQLAlchemyPoller(poller_config)
        await base_poller()

        cleanup_config = SQLAlchemyPollerWithCleanupConfig(
            poller=base_poller,
            cleanup_interval=timedelta(seconds=0),
            cleanup_older_than=timedelta(seconds=0.1),
        )

        subject = SQLAlchemyPollerWithCleanup(cleanup_config)

        message_id = uuid.uuid4()
        await subject.push(message_id, data={"test": True})

        async with async_session_factory() as session:
            count_before = len((await session.execute(select(base_poller.table))).all())
        assert count_before == 1

        await subject.peek(message_id)

        async with async_session_factory() as session:
            count_after = len((await session.execute(select(base_poller.table))).all())
        assert count_after == 1

    async def test_cleanup_respects_interval(self, db_engine: AsyncEngine):
        async_session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
        poller_config = SQLAlchemyPollerConfig(
            table_name=f"polling_results_{uuid.uuid4().hex[:8]}",
            async_session_factory=async_session_factory,
            poll_interval=0.05,
        )

        base_poller = SQLAlchemyPoller(poller_config)
        await base_poller()

        cleanup_config = SQLAlchemyPollerWithCleanupConfig(
            poller=base_poller,
            cleanup_interval=timedelta(hours=1),
            cleanup_older_than=timedelta(seconds=10),
        )

        subject = SQLAlchemyPollerWithCleanup(cleanup_config)

        msg1 = uuid.uuid4()
        await subject.push(msg1, data={"msg": 1})

        async def push_after_delay(msg_id, delay):
            await asyncio.sleep(delay)
            await subject.push(msg_id, data={"msg": str(msg_id)})

        msg2 = uuid.uuid4()
        task2 = asyncio.create_task(push_after_delay(msg2, 0.1))
        await subject.poll(msg2)
        await task2

        msg3 = uuid.uuid4()
        task3 = asyncio.create_task(push_after_delay(msg3, 0.1))
        await subject.poll(msg3)
        await task3

        async with async_session_factory() as session:
            count = len((await session.execute(select(base_poller.table))).all())
        assert count == 3
