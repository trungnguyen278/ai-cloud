# SPDX-License-Identifier: MIT
"""The accounts' outbox, and the sequence that gives its events their places in their stream
(docs/decisions/0009-domain-events.md).

Revision: accounts_0004, after accounts_0003
"""

import sqlalchemy as sa
from alembic import op

revision = "accounts_0004"
down_revision = "accounts_0003"
branch_labels = None
depends_on = None

SCHEMA = "accounts"


def upgrade() -> None:
    op.create_table(
        "outbox",
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("position", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("seq", name="pk_outbox"),
        sa.UniqueConstraint("position", name="uq_outbox_position"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_outbox_unnumbered",
        "outbox",
        ["seq"],
        schema=SCHEMA,
        postgresql_where=sa.text("position IS NULL"),
    )
    op.execute(sa.schema.CreateSequence(sa.Sequence("outbox_position", schema=SCHEMA)))


def downgrade() -> None:
    op.execute(sa.schema.DropSequence(sa.Sequence("outbox_position", schema=SCHEMA)))
    op.drop_table("outbox", schema=SCHEMA)
