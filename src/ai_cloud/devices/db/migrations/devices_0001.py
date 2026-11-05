# SPDX-License-Identifier: MIT
"""The registry of devices and their keys, the challenges to register one, and the first
answers to registrations.

Revision: devices_0001, the first of the devices' branch
"""

import sqlalchemy as sa
from alembic import op

revision = "devices_0001"
down_revision = None
branch_labels = ("devices",)
depends_on = None

SCHEMA = "devices"


def upgrade() -> None:
    op.create_table(
        "devices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("home_id", sa.Uuid(), nullable=False),
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_devices"),
        schema=SCHEMA,
    )
    op.create_index("ix_devices_home_id_id", "devices", ["home_id", "id"], schema=SCHEMA)
    op.create_table(
        "keys",
        sa.Column("thumbprint", sa.Text(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("jwk", sa.JSON(), nullable=False),
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["device_id"],
            [f"{SCHEMA}.devices.id"],
            name="fk_keys_device_id_devices",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("thumbprint", name="pk_keys"),
        schema=SCHEMA,
    )
    op.create_index("ix_devices_keys_device_id", "keys", ["device_id"], schema=SCHEMA)
    op.create_table(
        "challenges",
        sa.Column("hash", sa.LargeBinary(), nullable=False),
        sa.Column("home_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expire_time", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("hash", name="pk_challenges"),
        schema=SCHEMA,
    )
    op.create_index("ix_devices_challenges_home_id", "challenges", ["home_id"], schema=SCHEMA)
    op.create_index("ix_devices_challenges_account_id", "challenges", ["account_id"], schema=SCHEMA)
    op.create_table(
        "idempotency_keys",
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.Uuid(), nullable=False),
        sa.Column("fingerprint", sa.LargeBinary(), nullable=False),
        sa.Column("home_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.SmallInteger(), nullable=False),
        sa.Column("answer", sa.JSON(), nullable=False),
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["device_id"],
            [f"{SCHEMA}.devices.id"],
            name="fk_idempotency_keys_device_id_devices",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("account_id", "key", name="pk_idempotency_keys"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_devices_idempotency_keys_home_id", "idempotency_keys", ["home_id"], schema=SCHEMA
    )
    op.create_index(
        "ix_devices_idempotency_keys_device_id", "idempotency_keys", ["device_id"], schema=SCHEMA
    )


def downgrade() -> None:
    op.drop_table("idempotency_keys", schema=SCHEMA)
    op.drop_table("challenges", schema=SCHEMA)
    op.drop_table("keys", schema=SCHEMA)
    op.drop_table("devices", schema=SCHEMA)
