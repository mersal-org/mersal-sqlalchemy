import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Identity,
    Integer,
    LargeBinary,
    String,
    Table,
    inspect,
)
from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session, registry
from sqlalchemy.types import JSON

__all__ = (
    "create_outbox_table_and_map",
    "create_polling_results_table",
    "create_sagas_table",
    "ensure_table_exists",
)


JsonB = JSON().with_variant(PG_JSONB, "postgresql")


def ensure_table_exists(table: Table, sync_session: Session) -> None:
    """Create ``table`` if it doesn't exist yet, tolerating concurrent creators."""
    # A REPEATABLE READ (or SERIALIZABLE) caller freezes this transaction's
    # snapshot at its first statement. If a concurrent creator wins the race
    # below, our post-failure `has_table` recheck would still be looking at
    # the pre-race snapshot and wrongly conclude the table is genuinely
    # missing, re-raising a race we actually recovered from. Forcing READ
    # COMMITTED for this bootstrap-only transaction gives every statement a
    # fresh snapshot so the recheck can see the winner's commit.
    execution_options = {}
    if sync_session.get_bind().dialect.name == "postgresql":
        execution_options["isolation_level"] = "READ COMMITTED"
    connection = sync_session.connection(execution_options=execution_options)
    if inspect(connection).has_table(table.name, schema=table.schema):
        return

    savepoint = connection.begin_nested()
    try:
        table.create(connection, checkfirst=False)
    except Exception:
        savepoint.rollback()
        if not inspect(connection).has_table(table.name, schema=table.schema):
            raise
    else:
        savepoint.commit()


def create_outbox_table_and_map(
    table_name: str,
    mapper_registry: registry,
) -> Table:
    metadata = mapper_registry.metadata
    table: Table | None = None
    for _table in metadata.sorted_tables:
        if _table.name == table_name:
            table = _table
            break
    if table is None:
        table = Table(
            table_name,
            metadata,
            Column(
                "outbox_message_id",
                BigInteger().with_variant(Integer, "sqlite"),
                Identity(always=True, start=1),
                primary_key=True,
            ),
            Column("destination_address", String, nullable=False),
            Column("body", LargeBinary, nullable=False),
            Column("headers", LargeBinary, nullable=False),
            Column("sent", Boolean, nullable=False, default=False),
        )

    return table


def create_sagas_table(table_name: str, mapper_registry: registry) -> Table:
    metadata = mapper_registry.metadata
    table: Table | None = None
    for _table in metadata.sorted_tables:
        if _table.name == table_name:
            table = _table
            break
    if table is None:
        table = Table(
            table_name,
            metadata,
            Column("id", PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
            Column("revision", Integer, nullable=False),
            Column("data", JsonB, nullable=False),
            Column("saga_type", String, nullable=False),
        )

    return table


def create_polling_results_table(table_name: str, mapper_registry: registry) -> Table:
    metadata = mapper_registry.metadata
    table: Table | None = None
    for _table in metadata.sorted_tables:
        if _table.name == table_name:
            table = _table
            break
    if table is None:
        table = Table(
            table_name,
            metadata,
            Column("message_id", String, primary_key=True),
            Column("status", String, nullable=False),
            Column("data", JsonB, nullable=True),
            Column("problem", JsonB, nullable=True),
            Column(
                "created_at",
                DateTime(timezone=True),
                nullable=False,
                default=lambda: datetime.now(timezone.utc),
            ),
        )

    return table
