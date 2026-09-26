import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    inspect,
)
from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session
from sqlalchemy.types import JSON

__all__ = (
    "MissingTableError",
    "create_outbox_table_and_map",
    "create_polling_results_table",
    "create_sagas_table",
    "create_timeouts_table",
    "ensure_table_exists",
    "prepare_table",
    "verify_table_exists",
)


JsonB = JSON().with_variant(PG_JSONB, "postgresql")


class MissingTableError(RuntimeError):
    """Raised on startup when ``auto_create_table`` is off and the table doesn't exist."""

    def __init__(self, table: Table) -> None:
        super().__init__(
            f"Table {table.fullname!r} does not exist and auto_create_table is disabled. "
            "Create it through your migrations (the table builders in mersal.sqlalchemy.orm "
            "can add it to your app's MetaData so Alembic autogenerate picks it up), "
            "or enable auto_create_table."
        )
        self.table = table


def prepare_table(table: Table, sync_session: Session, *, auto_create: bool) -> None:
    """Create ``table`` if ``auto_create`` is set, otherwise only check it exists."""
    if auto_create:
        ensure_table_exists(table, sync_session)
    else:
        verify_table_exists(table, sync_session)


def verify_table_exists(table: Table, sync_session: Session) -> None:
    """Raise `MissingTableError` if ``table`` doesn't exist. Never issues DDL."""
    if not inspect(sync_session.connection()).has_table(table.name, schema=table.schema):
        raise MissingTableError(table)


def _existing_table(metadata: MetaData, table_name: str, schema: str | None) -> Table | None:
    schema = schema or metadata.schema
    return metadata.tables.get(f"{schema}.{table_name}" if schema else table_name)


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
    metadata: MetaData | None = None,
    schema: str | None = None,
) -> Table:
    """Define the outbox table on ``metadata`` (a fresh one if omitted), or return it if already defined.

    Pass your app's ``MetaData`` to include the table in Alembic autogenerate. As with
    any ``Table``, ``schema=None`` falls back to the metadata's own default schema, so
    pass ``schema`` explicitly when the table lives elsewhere (e.g. ``"public"``).
    """
    metadata = MetaData() if metadata is None else metadata
    table = _existing_table(metadata, table_name, schema)
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
            schema=schema,
        )

    return table


def create_sagas_table(
    table_name: str,
    metadata: MetaData | None = None,
    schema: str | None = None,
) -> Table:
    """Define the saga storage table on ``metadata`` (a fresh one if omitted), or return it if already defined.

    Pass your app's ``MetaData`` to include the table in Alembic autogenerate. As with
    any ``Table``, ``schema=None`` falls back to the metadata's own default schema, so
    pass ``schema`` explicitly when the table lives elsewhere (e.g. ``"public"``).
    """
    metadata = MetaData() if metadata is None else metadata
    table = _existing_table(metadata, table_name, schema)
    if table is None:
        table = Table(
            table_name,
            metadata,
            Column("id", PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
            Column("revision", Integer, nullable=False),
            Column("data", JsonB, nullable=False),
            Column("saga_type", String, nullable=False),
            schema=schema,
        )

    return table


def create_polling_results_table(
    table_name: str,
    metadata: MetaData | None = None,
    schema: str | None = None,
) -> Table:
    """Define the polling results table on ``metadata`` (a fresh one if omitted), or return it if already defined.

    Pass your app's ``MetaData`` to include the table in Alembic autogenerate. As with
    any ``Table``, ``schema=None`` falls back to the metadata's own default schema, so
    pass ``schema`` explicitly when the table lives elsewhere (e.g. ``"public"``).
    """
    metadata = MetaData() if metadata is None else metadata
    table = _existing_table(metadata, table_name, schema)
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
            schema=schema,
        )

    return table


def create_timeouts_table(
    table_name: str,
    metadata: MetaData | None = None,
    schema: str | None = None,
) -> Table:
    """Define the timeouts table on ``metadata`` (a fresh one if omitted), or return it if already defined.

    Pass your app's ``MetaData`` to include the table in Alembic autogenerate. As with
    any ``Table``, ``schema=None`` falls back to the metadata's own default schema, so
    pass ``schema`` explicitly when the table lives elsewhere (e.g. ``"public"``).
    """
    metadata = MetaData() if metadata is None else metadata
    table = _existing_table(metadata, table_name, schema)
    if table is None:
        table = Table(
            table_name,
            metadata,
            Column(
                "id",
                BigInteger().with_variant(Integer, "sqlite"),
                Identity(always=True, start=1),
                primary_key=True,
            ),
            Column("due_time", DateTime(timezone=True), nullable=False),
            Column("headers", JsonB, nullable=False),
            Column("body", LargeBinary, nullable=False),
            Index(f"ix_{table_name}_due_time", "due_time"),
            schema=schema,
        )

    return table
