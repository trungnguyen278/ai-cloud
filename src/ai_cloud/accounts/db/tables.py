# SPDX-License-Identifier: MIT
"""The accounts' tables, in the ``accounts`` schema. Their migrations are in
``migrations/``, beside them."""

from pathlib import Path

from sqlalchemy import (
    ARRAY,
    JSON,
    BigInteger,
    CheckConstraint,
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

homes = Table(
    "homes",
    tables,
    Column("id", Uuid, primary_key=True),
    Column("name", Text, nullable=False),
    Column("time_zone", Text, nullable=False),
    Column("languages", ARRAY(Text), nullable=False),
    Column("version", BigInteger, nullable=False),
    Column("create_time", DateTime(timezone=True), nullable=False),
)

members = Table(
    "members",
    tables,
    Column("id", Uuid, primary_key=True),
    Column("home_id", Uuid, ForeignKey(homes.c.id, ondelete="CASCADE"), nullable=False),
    # None for a profile. An account's deletion does not cascade here: it decides, home by
    # home, what goes with it, and a person it forgot stops it.
    Column("account_id", Uuid, ForeignKey(accounts.c.id), index=True),
    Column("display_name", Text, nullable=False),
    Column("role", Text),
    Column("version", BigInteger, nullable=False),
    Column("create_time", DateTime(timezone=True), nullable=False),
    UniqueConstraint("home_id", "account_id"),
    # A person with an account has a role; a profile has none.
    CheckConstraint("(account_id IS NULL) = (role IS NULL)", name="role_with_account"),
    # A home has one owner at most: handing it over demotes the old one first.
    Index("ix_members_owner", "home_id", unique=True, postgresql_where=text("role = 'owner'")),
)

invitations = Table(
    "invitations",
    tables,
    Column("id", Uuid, primary_key=True),
    # The code's SHA-256: the code itself is never kept.
    Column("code_hash", LargeBinary, nullable=False, unique=True),
    Column("home_id", Uuid, ForeignKey(homes.c.id, ondelete="CASCADE"), nullable=False, index=True),
    # The person who made it: removing them revokes it.
    Column(
        "invited_by", Uuid, ForeignKey(members.c.id, ondelete="CASCADE"), nullable=False, index=True
    ),
    Column("create_time", DateTime(timezone=True), nullable=False),
    Column("expire_time", DateTime(timezone=True), nullable=False),
    # The person it brought in; none while it is unused. It is kept until it expires, so
    # that a retry of joining ends in the same place, and goes if that person leaves.
    Column("used_by", Uuid, ForeignKey(members.c.id, ondelete="CASCADE"), index=True),
)

# The first answers to requests with an Idempotency-Key. A retry gets one for as long as the
# configuration's idempotency_retention says; sweeping older ones is the worker's job.
idempotency_keys = Table(
    "idempotency_keys",
    tables,
    Column("account_id", Uuid, ForeignKey(accounts.c.id, ondelete="CASCADE"), primary_key=True),
    Column("key", Uuid, primary_key=True),
    # The SHA-256 of what the request asked: its method, its path and its body.
    Column("fingerprint", LargeBinary, nullable=False),
    # The person the request made, or who made the home: an answer holds their names, so it
    # goes with them.
    Column(
        "member_id", Uuid, ForeignKey(members.c.id, ondelete="CASCADE"), nullable=False, index=True
    ),
    Column("answer", JSON, nullable=False),
    Column("create_time", DateTime(timezone=True), nullable=False),
)

MIGRATIONS = Migrations(
    schema=SCHEMA, metadata=tables, directory=Path(__file__).parent / "migrations"
)
