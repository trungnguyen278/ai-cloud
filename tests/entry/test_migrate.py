# SPDX-License-Identifier: MIT
"""The migrations, each test on a new database beside the tests' own."""

import asyncio
import shutil
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Column, Connection, Text, inspect, text

from ai_cloud.accounts.db.tables import MIGRATIONS as ACCOUNTS
from ai_cloud.entry.migrate import migrate, revise
from ai_cloud.shared.database import DatabaseSettings, create_engine, database_url, metadata

NAME = "migrations_test"


def _on(url: str, settings: DatabaseSettings, work: Callable[[Connection], Any]) -> Any:
    """``work``'s result, on a connection to ``url``, outside any transaction."""

    async def run() -> Any:
        engine = create_engine(url, settings, "ai-cloud tests")
        try:
            async with engine.connect() as connection:
                await connection.execution_options(isolation_level="AUTOCOMMIT")
                return await connection.run_sync(work)
        finally:
            await engine.dispose()

    return asyncio.run(run())


@pytest.fixture
def settings(make_settings: Any) -> DatabaseSettings:
    return make_settings().database


@pytest.fixture
def fresh(postgres_url: str, settings: DatabaseSettings) -> Iterator[str]:
    """A new, empty database's URL."""

    def admin(statement: str) -> None:
        _on(postgres_url, settings, lambda connection: connection.execute(text(statement)))

    admin(f"DROP DATABASE IF EXISTS {NAME} WITH (FORCE)")
    admin(f"CREATE DATABASE {NAME}")
    try:
        yield database_url(postgres_url).set(database=NAME).render_as_string(hide_password=False)
    finally:
        admin(f"DROP DATABASE IF EXISTS {NAME} WITH (FORCE)")


def _state(connection: Connection) -> tuple[list[str], list[Any], list[str]]:
    """The accounts' tables, what they lack of what the module declares, and the
    branch's version."""
    context = MigrationContext.configure(
        connection, opts={"include_schemas": True, "version_table_schema": ACCOUNTS.schema}
    )
    differences = compare_metadata(context, ACCOUNTS.metadata)
    tables = sorted(inspect(connection).get_table_names(schema=ACCOUNTS.schema))
    versions = [
        row[0] for row in connection.execute(text("SELECT * FROM accounts.alembic_version"))
    ]
    return tables, differences, versions


def test_every_module_migrates_to_what_its_tables_declare(
    fresh: str, settings: DatabaseSettings
) -> None:
    asyncio.run(migrate(fresh, settings))
    tables, differences, versions = _on(fresh, settings, _state)
    assert tables == ["accounts", "alembic_version", "identities", "refresh_tokens", "sessions"]
    assert differences == []
    # Its version table is in its own schema, and holds its branch alone.
    assert versions == ["accounts_0001"]
    assert (
        _on(
            fresh,
            settings,
            lambda connection: inspect(connection).get_table_names(schema="public"),
        )
        == []
    )
    # Nothing to do the second time.
    asyncio.run(migrate(fresh, settings))
    assert _on(fresh, settings, _state)[2] == versions


def test_a_revision_is_written_from_what_the_tables_lack(
    fresh: str, settings: DatabaseSettings, tmp_path: Path
) -> None:
    asyncio.run(migrate(fresh, settings))
    directory = Path(shutil.copytree(ACCOUNTS.directory, tmp_path / "migrations"))
    tables = metadata(ACCOUNTS.schema)
    for table in ACCOUNTS.metadata.tables.values():
        table.to_metadata(tables)
    tables.tables["accounts.accounts"].append_column(Column("pronouns", Text))
    module = replace(ACCOUNTS, metadata=tables, directory=directory)
    written = asyncio.run(revise(fresh, settings, module, "Add pronouns", modules=[module]))
    assert written.parent == directory
    assert written.name.startswith("accounts_0002_")
    source = written.read_text(encoding="utf-8")
    assert source.startswith("# SPDX-License-Identifier: MIT\n")
    assert "revision = 'accounts_0002'" in source
    assert "down_revision = 'accounts_0001'" in source
    assert "op.add_column('accounts', sa.Column('pronouns'" in source
