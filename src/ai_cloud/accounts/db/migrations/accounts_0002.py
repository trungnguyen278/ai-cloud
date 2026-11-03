# SPDX-License-Identifier: MIT
"""Homes, the people in them, their invitations, and the first answers to retried requests.

Revision: accounts_0002, after accounts_0001
"""

import sqlalchemy as sa
from alembic import op

revision = "accounts_0002"
down_revision = "accounts_0001"
branch_labels = None
depends_on = None

SCHEMA = "accounts"


def upgrade() -> None:
    op.create_table(
        "homes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("time_zone", sa.Text(), nullable=False),
        sa.Column("languages", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_homes"),
        schema=SCHEMA,
    )
    op.create_table(
        "members",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("home_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(account_id IS NULL) = (role IS NULL)", name="ck_members_role_with_account"
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], [f"{SCHEMA}.accounts.id"], name="fk_members_account_id_accounts"
        ),
        sa.ForeignKeyConstraint(
            ["home_id"],
            [f"{SCHEMA}.homes.id"],
            name="fk_members_home_id_homes",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_members"),
        sa.UniqueConstraint("home_id", "account_id", name="uq_members_home_id_account_id"),
        schema=SCHEMA,
    )
    op.create_index("ix_accounts_members_account_id", "members", ["account_id"], schema=SCHEMA)
    op.create_index(
        "ix_members_owner",
        "members",
        ["home_id"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("role = 'owner'"),
    )
    op.create_table(
        "invitations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code_hash", sa.LargeBinary(), nullable=False),
        sa.Column("home_id", sa.Uuid(), nullable=False),
        sa.Column("invited_by", sa.Uuid(), nullable=False),
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expire_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["home_id"],
            [f"{SCHEMA}.homes.id"],
            name="fk_invitations_home_id_homes",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["invited_by"],
            [f"{SCHEMA}.members.id"],
            name="fk_invitations_invited_by_members",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["used_by"],
            [f"{SCHEMA}.members.id"],
            name="fk_invitations_used_by_members",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_invitations"),
        sa.UniqueConstraint("code_hash", name="uq_invitations_code_hash"),
        schema=SCHEMA,
    )
    op.create_index("ix_accounts_invitations_home_id", "invitations", ["home_id"], schema=SCHEMA)
    op.create_index(
        "ix_accounts_invitations_invited_by", "invitations", ["invited_by"], schema=SCHEMA
    )
    op.create_index("ix_accounts_invitations_used_by", "invitations", ["used_by"], schema=SCHEMA)
    op.create_table(
        "idempotency_keys",
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.Uuid(), nullable=False),
        sa.Column("fingerprint", sa.LargeBinary(), nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=False),
        sa.Column("answer", sa.JSON(), nullable=False),
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"],
            [f"{SCHEMA}.accounts.id"],
            name="fk_idempotency_keys_account_id_accounts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["member_id"],
            [f"{SCHEMA}.members.id"],
            name="fk_idempotency_keys_member_id_members",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("account_id", "key", name="pk_idempotency_keys"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_accounts_idempotency_keys_member_id", "idempotency_keys", ["member_id"], schema=SCHEMA
    )


def downgrade() -> None:
    op.drop_table("idempotency_keys", schema=SCHEMA)
    op.drop_table("invitations", schema=SCHEMA)
    op.drop_table("members", schema=SCHEMA)
    op.drop_table("homes", schema=SCHEMA)
