# SPDX-License-Identifier: MIT
"""PostgreSQL, through SQLAlchemy's Core on asyncpg (docs/decisions/0008-data-and-tokens.md).

Each module declares its tables in a schema of its own, with ``metadata(schema)``, and
keeps its migrations beside them; ``ai-cloud migrate`` runs them all through the one
Alembic environment in ``migrations/``. Queries are SQL expressions, written where they
run. There are no ORM sessions.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from pydantic import Field
from sqlalchemy import MetaData
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ai_cloud.shared.config import ConfigError, Settings

DATABASE_URL_ENV = "AI_CLOUD_DATABASE_URL"

# The driver: the URL in the environment names the database only (postgresql://).
DRIVER = "postgresql+asyncpg"

# Alembic's environment, which runs every module's migrations.
MIGRATIONS_ENVIRONMENT = Path(__file__).parent / "migrations"

# Names for constraints and indexes, so that a migration names them alike on every
# database (SQLAlchemy, "Configuring Constraint Naming Conventions").
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class DatabaseSettings(Settings):
    # Connections each process keeps open.
    pool_size: Annotated[int, Field(ge=1)]
    # Connections it may open beyond those, when they are all busy.
    max_overflow: Annotated[int, Field(ge=0)]
    # Seconds a request waits for a free connection before it fails.
    pool_timeout: Annotated[float, Field(gt=0)]
    # Seconds to open a connection.
    connect_timeout: Annotated[float, Field(gt=0)]
    # Seconds a statement may run before it is cancelled.
    command_timeout: Annotated[float, Field(gt=0)]


@dataclass(frozen=True)
class Migrations:
    """A module's tables and migrations, for ``ai-cloud migrate``."""

    # The module's schema, which is also its branch's label in Alembic.
    schema: str
    metadata: MetaData
    # Where its revisions are.
    directory: Path


def metadata(schema: str) -> MetaData:
    """The metadata a module declares its tables on, in its own ``schema``."""
    return MetaData(schema=schema, naming_convention=NAMING_CONVENTION)


def database_url(url: str) -> URL:
    """``url``, a ``postgresql://`` URL from the environment, with the cloud's driver.
    ``ConfigError`` if it is not one, without the URL, which holds a password."""
    try:
        parsed = make_url(url)
    except ArgumentError, ValueError:
        raise ConfigError(f"{DATABASE_URL_ENV} is not a URL") from None
    if parsed.drivername not in ("postgresql", "postgres", DRIVER):
        raise ConfigError(f"{DATABASE_URL_ENV} must be a postgresql:// URL")
    return parsed.set(drivername=DRIVER)


def create_engine(url: str, settings: DatabaseSettings, application_name: str) -> AsyncEngine:
    """An engine for the database at ``url``. It connects when it is first used, and
    ``application_name`` names its connections in PostgreSQL's own views."""
    return create_async_engine(
        database_url(url),
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        pool_timeout=settings.pool_timeout,
        # A connection that the server closed, after a restart say, is replaced before a
        # request gets it.
        pool_pre_ping=True,
        connect_args={
            "timeout": settings.connect_timeout,
            "command_timeout": settings.command_timeout,
            "server_settings": {"application_name": application_name},
        },
    )
