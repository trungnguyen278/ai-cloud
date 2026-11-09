# SPDX-License-Identifier: MIT
"""The ``migrate`` role: brings every module's tables to its newest migration, as a step
of a deployment, before the new processes start; never a process as it starts
(docs/decisions/0008-data-and-tokens.md, decision 2).

The modules' migrations are branches of one Alembic environment
(``shared/migrations``), each labelled with its module's schema, with its version table
in that schema.
"""

import logging
import os
from collections.abc import Sequence
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection

from ai_cloud.accounts.api import MIGRATIONS as ACCOUNTS
from ai_cloud.devices.api import MIGRATIONS as DEVICES
from ai_cloud.shared.database import (
    MIGRATIONS_ENVIRONMENT,
    DatabaseSettings,
    Migrations,
    create_engine,
)

APPLICATION_NAME = "ai-cloud migrate"  # how its connection appears in PostgreSQL

# Every module with tables, in the order they migrate.
MODULES: tuple[Migrations, ...] = (ACCOUNTS, DEVICES)

log = logging.getLogger(__name__)


async def migrate(
    url: str, settings: DatabaseSettings, modules: Sequence[Migrations] = MODULES
) -> None:
    """Bring each of ``modules`` to its newest migration, each in a transaction."""
    engine = create_engine(url, settings, APPLICATION_NAME)
    try:
        for module in modules:
            async with engine.begin() as connection:
                await connection.run_sync(_upgrade, module, modules)
            log.info("migrated", extra={"branch": module.schema})
    finally:
        await engine.dispose()


async def revise(
    url: str,
    settings: DatabaseSettings,
    module: Migrations,
    message: str,
    modules: Sequence[Migrations] = MODULES,
) -> Path:
    """Write ``module``'s next migration, from what its tables declare and the database,
    at its newest migration, lacks; return its file, to read through before it is
    committed."""
    engine = create_engine(url, settings, APPLICATION_NAME)
    try:
        async with engine.begin() as connection:
            return await connection.run_sync(_revise, module, message, modules)
    finally:
        await engine.dispose()


def alembic_config(
    module: Migrations, connection: Connection, modules: Sequence[Migrations]
) -> Config:
    """Alembic's configuration for ``module``, one branch of ``modules``, on
    ``connection``."""
    config = Config()
    # Alembic's options are an INI file's, in which % is special.
    config.set_main_option("script_location", str(MIGRATIONS_ENVIRONMENT).replace("%", "%%"))
    config.set_main_option("path_separator", "os")
    config.set_main_option(
        "version_locations",
        os.pathsep.join(str(each.directory).replace("%", "%%") for each in modules),
    )
    config.attributes.update(connection=connection, schema=module.schema, metadata=module.metadata)
    return config


def _upgrade(connection: Connection, module: Migrations, modules: Sequence[Migrations]) -> None:
    command.upgrade(alembic_config(module, connection, modules), f"{module.schema}@head")


def _revise(
    connection: Connection, module: Migrations, message: str, modules: Sequence[Migrations]
) -> Path:
    config = alembic_config(module, connection, modules)
    branch = list(
        ScriptDirectory.from_config(config).walk_revisions("base", f"{module.schema}@head")
    )
    written = command.revision(
        config,
        message=message,
        autogenerate=True,
        head=f"{module.schema}@head",
        version_path=str(module.directory),
        rev_id=f"{module.schema}_{len(branch) + 1:04d}",
    )
    assert written is not None and not isinstance(written, list)
    return Path(written.path)
