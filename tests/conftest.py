# pyright: reportWildcardImportFromLibrary=false

import asyncio
import os
import re
import subprocess
import timeit
from collections.abc import Awaitable, Callable, Generator
from pathlib import Path
from typing import Any

import asyncpg
import msgspec
import pytest
from sqlalchemy import NullPool, event
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mersal.testing.core._internal.conftest import *
from mersal.utils import AsyncCallable

__all__ = (
    "DockerServiceRegistry",
    "docker_ip",
    "docker_services",
    "fx_db_config",
    "fx_engine",
    "fx_engine_with_msgspec_serializer",
    "postgres_responsive",
    "postgres_service",
    "wait_until_responsive",
)


class TimeoutError(Exception):
    """Service response timed out."""


async def wait_until_responsive(
    check: Callable[..., Awaitable],
    timeout: float,
    pause: float,
    **kwargs: Any,
) -> None:
    """Wait until a service is responsive.

    Args:
        check: Coroutine, return truthy value when waiting should stop.
        timeout: Maximum seconds to wait.
        pause: Seconds to wait between calls to `check`.
        **kwargs: Given as kwargs to `check`.
    """
    ref = timeit.default_timer()
    now = ref
    while (now - ref) < timeout:
        if await check(**kwargs):
            return
        await asyncio.sleep(pause)
        now = timeit.default_timer()

    raise TimeoutError("Timeout reached while waiting on service!")


class DockerServiceRegistry:
    def __init__(self) -> None:
        self._running_services: set[str] = set()
        self.docker_ip = self._get_docker_ip()
        file_name = Path(__file__).resolve().parent / "docker-compose.yml"
        self._base_command = [
            "docker",
            "compose",
            f"--file={file_name!s}",
            "--project-name=mersal_pytest",
        ]

    def _get_docker_ip(self) -> str:
        docker_host = os.environ.get("DOCKER_HOST", "").strip()
        if not docker_host or docker_host.startswith("unix://"):
            return "127.0.0.1"

        match = re.match(r"^tcp://(.+?):\d+$", docker_host)
        if not match:
            raise ValueError(f'Invalid value for DOCKER_HOST: "{docker_host}".')
        return match.group(1)

    def run_command(self, *args: str) -> None:
        subprocess.run([*self._base_command, *args], check=True, capture_output=True)

    def _capture_command(self, *args: str) -> str:
        result = subprocess.run(
            [*self._base_command, *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout + result.stderr

    def _diagnostics(self, name: str) -> str:
        ps_output = self._capture_command("ps", "-a")
        logs_output = self._capture_command("logs", name)
        return f"docker compose ps -a:\n{ps_output}\ndocker compose logs {name}:\n{logs_output}"

    async def start(
        self,
        name: str,
        *,
        check: Callable[..., Awaitable],
        timeout: float = 30,
        pause: float = 0.1,
        **kwargs: Any,
    ) -> None:
        if name not in self._running_services:
            self.run_command("down", name)
            self.run_command("up", "-d", name)
            self._running_services.add(name)

            try:
                await wait_until_responsive(
                    check=AsyncCallable(check),
                    timeout=timeout,
                    pause=pause,
                    host=self.docker_ip,
                    **kwargs,
                )
            except TimeoutError as e:
                raise TimeoutError(f"{e}\n\n{self._diagnostics(name)}") from e

    def stop(self, name: str) -> None:
        pass

    def down(self) -> None:
        self.run_command("down", "-t", "5")


@pytest.fixture(scope="session")
def docker_services() -> Generator[DockerServiceRegistry, None, None]:
    registry = DockerServiceRegistry()
    yield registry
    registry.down()


@pytest.fixture(scope="session")
def docker_ip(docker_services: DockerServiceRegistry) -> str:
    return docker_services.docker_ip


async def postgres_responsive(host: str) -> bool:
    try:
        conn = await asyncpg.connect(
            host=host,
            port=5423,
            user="postgres",
            database="postgres",
            password="super-secret",
        )
    except (ConnectionError, asyncpg.CannotConnectNowError):
        return False

    try:
        return (await conn.fetchrow("SELECT 1"))[0] == 1  # type: ignore
    finally:
        await conn.close()


@pytest.fixture()
async def postgres_service(docker_services: DockerServiceRegistry) -> None:
    await docker_services.start("postgres", check=postgres_responsive)


@pytest.fixture(name="db_config")
def fx_db_config(docker_ip: str) -> dict:
    return {
        "url": URL(
            drivername="postgresql+asyncpg",
            username="postgres",
            password="super-secret",
            host=docker_ip,
            port=5423,
            database="postgres",
            query={},  # type:ignore[arg-type]
        ),
        "echo": True,
        "poolclass": NullPool,
    }


@pytest.fixture(name="db_engine")
async def fx_engine(db_config: dict) -> AsyncEngine:
    return create_async_engine(**db_config)


@pytest.fixture(name="db_engine_msgspec")
async def fx_engine_with_msgspec_serializer(db_config: dict) -> AsyncEngine:
    engine = create_async_engine(
        **db_config,
        json_serializer=msgspec.json.encode,
        json_deserializer=msgspec.json.decode,
    )
    event.listen(engine.sync_engine, "connect", _sqla_on_connect)

    return engine


def _sqla_on_connect(dbapi_connection: Any, _: Any) -> Any:  # pragma: no cover
    def encoder(bin_value: bytes) -> bytes:
        return b"\x01" + bin_value

    def decoder(bin_value: bytes) -> Any:
        return msgspec.json.decode(bin_value[1:])

    dbapi_connection.await_(
        dbapi_connection.driver_connection.set_type_codec(
            "jsonb",
            encoder=encoder,
            decoder=decoder,
            schema="pg_catalog",
            format="binary",
        )
    )
    dbapi_connection.await_(
        dbapi_connection.driver_connection.set_type_codec(
            "json",
            encoder=encoder,
            decoder=decoder,
            schema="pg_catalog",
            format="binary",
        )
    )
