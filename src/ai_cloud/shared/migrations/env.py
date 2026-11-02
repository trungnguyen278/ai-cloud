# SPDX-License-Identifier: MIT
"""Alembic's environment: one module's migrations, on the connection that ``ai-cloud
migrate`` gives it (docs/decisions/0008-data-and-tokens.md, decision 2).

Every module is a branch of this one environment, labelled with its schema. Its version
table is in its schema too, so that the module can move to a database of its own.
``config.attributes`` brings the connection, the module's schema and its metadata.
"""

from typing import Any

from alembic import context
from sqlalchemy.schema import CreateSchema

if context.is_offline_mode():
    raise RuntimeError("migrations run on a database, through `ai-cloud migrate`")

config = context.config
connection = config.attributes["connection"]
schema: str = config.attributes["schema"]


def include_name(name: str | None, type_: str, _parents: dict[str, Any]) -> bool:
    """Autogenerate compares the module's own schema only."""
    return name == schema if type_ == "schema" else True


connection.execute(CreateSchema(schema, if_not_exists=True))
context.configure(
    connection=connection,
    target_metadata=config.attributes["metadata"],
    version_table_schema=schema,
    include_schemas=True,
    include_name=include_name,
)
with context.begin_transaction():
    context.run_migrations()
