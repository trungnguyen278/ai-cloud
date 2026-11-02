# SPDX-License-Identifier: MIT
"""The accounts' tables, in the ``accounts`` schema. Their migrations are in
``migrations/``, beside them."""

from pathlib import Path

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)

from ai_cloud.shared.database import Migrations, metadata

SCHEMA = "accounts"

tables = metadata(SCHEMA)

accounts = Table(
    "accounts",
    tables,
    Column("id", Uuid, primary_key=True),
    Column("display_name", Text, nullable=False),
    Column("email", Text),
    Column("languages", ARRAY(Text), nullable=False),
    Column("version", BigInteger, nullable=False),
    Column("create_time", DateTime(timezone=True), nullable=False),
)

identities = Table(
    "identities",
    tables,
    Column("id", Uuid, primary_key=True),
    Column(
        "account_id",
        Uuid,
        ForeignKey(accounts.c.id, ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    Column("provider", Text, nullable=False),
    # The provider's identifier for the person (its sub).
    Column("subject", Text, nullable=False),
    Column("email", Text),
    Column("create_time", DateTime(timezone=True), nullable=False),
    UniqueConstraint("provider", "subject"),
)

sessions = Table(
    "sessions",
    tables,
    Column("id", Uuid, primary_key=True),
    Column(
        "account_id",
        Uuid,
        ForeignKey(accounts.c.id, ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    Column("client_id", Text, nullable=False),
    Column("label", Text),
    Column("create_time", DateTime(timezone=True), nullable=False),
    Column("last_use_time", DateTime(timezone=True), nullable=False),
)

refresh_tokens = Table(
    "refresh_tokens",
    tables,
    # The token's SHA-256: the token itself is never kept.
    Column("hash", LargeBinary, primary_key=True),
    Column(
        "session_id",
        Uuid,
        ForeignKey(sessions.c.id, ondelete="CASCADE"),
        nullable=False,
    ),
    Column("create_time", DateTime(timezone=True), nullable=False),
    # When it was exchanged for the next one; none while it is its session's current one.
    Column("use_time", DateTime(timezone=True)),
    Index("ix_refresh_tokens_session_id", "session_id"),
    # A session has one current refresh token at most.
    Index(
        "ix_refresh_tokens_current",
        "session_id",
        unique=True,
        postgresql_where=text("use_time IS NULL"),
    ),
)

MIGRATIONS = Migrations(
    schema=SCHEMA, metadata=tables, directory=Path(__file__).parent / "migrations"
)
