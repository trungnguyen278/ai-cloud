# SPDX-License-Identifier: MIT
"""What each device's latest hello on the cloud link said it is: its kind, its model and its
software, which the app API shows (platform/protocols/app/README.md, Reading a home's
devices).

Revision: devices_0002, after devices_0001
"""

import sqlalchemy as sa
from alembic import op

revision = "devices_0002"
down_revision = "devices_0001"
branch_labels = None
depends_on = None

SCHEMA = "devices"


def upgrade() -> None:
    op.add_column("devices", sa.Column("kind", sa.Text(), nullable=True), schema=SCHEMA)
    op.add_column("devices", sa.Column("model", sa.Text(), nullable=True), schema=SCHEMA)
    op.add_column("devices", sa.Column("software", sa.Text(), nullable=True), schema=SCHEMA)


def downgrade() -> None:
    op.drop_column("devices", "software", schema=SCHEMA)
    op.drop_column("devices", "model", schema=SCHEMA)
    op.drop_column("devices", "kind", schema=SCHEMA)
