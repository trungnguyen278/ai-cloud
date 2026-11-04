# SPDX-License-Identifier: MIT
"""The token each refresh token replaced, so that a retry after a lost answer is known
(ai-platform ADR-0011, the app API 1.3).

Revision: accounts_0003, after accounts_0002
"""

import sqlalchemy as sa
from alembic import op

revision = "accounts_0003"
down_revision = "accounts_0002"
branch_labels = None
depends_on = None

SCHEMA = "accounts"


def upgrade() -> None:
    op.add_column(
        "refresh_tokens",
        sa.Column("replaces", sa.LargeBinary(), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("refresh_tokens", "replaces", schema=SCHEMA)
