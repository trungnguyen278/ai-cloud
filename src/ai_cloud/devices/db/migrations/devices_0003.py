# SPDX-License-Identifier: MIT
"""Each device's name in its home, and the version that its ETag names
(platform/protocols/app/README.md, Naming a device; ai-platform ADR-0013).

A device registered before has no name: an app of the app API 1.4 sent none, and the app shows
its own name for the device's kind until someone gives it one. Its version starts at 1, as a
new device's does.

Revision: devices_0003, after devices_0002
"""

import sqlalchemy as sa
from alembic import op

revision = "devices_0003"
down_revision = "devices_0002"
branch_labels = None
depends_on = None

SCHEMA = "devices"


def upgrade() -> None:
    op.add_column("devices", sa.Column("name", sa.Text(), nullable=True), schema=SCHEMA)
    # The default fills the rows there are, then goes: every new row names its version.
    op.add_column(
        "devices",
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        schema=SCHEMA,
    )
    op.alter_column("devices", "version", server_default=None, schema=SCHEMA)


def downgrade() -> None:
    op.drop_column("devices", "version", schema=SCHEMA)
    op.drop_column("devices", "name", schema=SCHEMA)
