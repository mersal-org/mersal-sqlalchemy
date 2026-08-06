from __future__ import annotations

from importlib.metadata import version

__all__ = [
    "SQLAlchemyOutboxStorage",
    "SQLAlchemyOutboxStorageConfig",
    "SQLAlchemyPoller",
    "SQLAlchemyPollerConfig",
    "SQLAlchemyPollerWithCleanup",
    "SQLAlchemyPollerWithCleanupConfig",
    "SQLAlchemySagaStorage",
    "SQLAlchemySagaStorageConfig",
    "SQLAlchemyUnitOfWork",
    "default_sqlalchemy_close_action",
    "default_sqlalchemy_commit_action",
    "default_sqlalchemy_rollback_action",
]

from .sqlalchemy_outbox_storage import (
    SQLAlchemyOutboxStorage,
    SQLAlchemyOutboxStorageConfig,
)
from .sqlalchemy_poller import SQLAlchemyPoller, SQLAlchemyPollerConfig
from .sqlalchemy_poller_with_cleanup import (
    SQLAlchemyPollerWithCleanup,
    SQLAlchemyPollerWithCleanupConfig,
)
from .sqlalchemy_saga_storage import SQLAlchemySagaStorage, SQLAlchemySagaStorageConfig
from .sqlalchemy_unit_of_work import (
    SQLAlchemyUnitOfWork,
    default_sqlalchemy_close_action,
    default_sqlalchemy_commit_action,
    default_sqlalchemy_rollback_action,
)


def __getattr__(name: str) -> str:
    if name != "__version__":
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)

    return version("mersal_sqlalchemy")
