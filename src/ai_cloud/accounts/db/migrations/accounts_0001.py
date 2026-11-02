# SPDX-License-Identifier: MIT
"""Accounts, their identities and sessions, and the sessions' refresh tokens.

Revision: accounts_0001, the first of the accounts' branch
"""

import sqlalchemy as sa
from alembic import op

revision = "accounts_0001"
down_revision = None
branch_labels = ("accounts",)
depends_on = None

SCHEMA = "accounts"


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("languages", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_accounts"),
        schema=SCHEMA,
    )
    op.create_table(
        "identities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"],
            [f"{SCHEMA}.accounts.id"],
            name="fk_identities_account_id_accounts",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_identities"),
        sa.UniqueConstraint("provider", "subject", name="uq_identities_provider_subject"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_accounts_identities_account_id", "identities", ["account_id"], schema=SCHEMA
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_use_time", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"],
            [f"{SCHEMA}.accounts.id"],
            name="fk_sessions_account_id_accounts",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sessions"),
        schema=SCHEMA,
    )
    op.create_index("ix_accounts_sessions_account_id", "sessions", ["account_id"], schema=SCHEMA)
    op.create_table(
        "refresh_tokens",
        sa.Column("hash", sa.LargeBinary(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("use_time", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["session_id"],
            [f"{SCHEMA}.sessions.id"],
            name="fk_refresh_tokens_session_id_sessions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("hash", name="pk_refresh_tokens"),
        schema=SCHEMA,
    )
    op.create_index("ix_refresh_tokens_session_id", "refresh_tokens", ["session_id"], schema=SCHEMA)
    op.create_index(
        "ix_refresh_tokens_current",
        "refresh_tokens",
        ["session_id"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("use_time IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("refresh_tokens", schema=SCHEMA)
    op.drop_table("sessions", schema=SCHEMA)
    op.drop_table("identities", schema=SCHEMA)
    op.drop_table("accounts", schema=SCHEMA)
