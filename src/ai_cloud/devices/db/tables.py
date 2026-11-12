# SPDX-License-Identifier: MIT
"""The devices' tables, in the ``devices`` schema. Their migrations are in ``migrations/``,
beside them.

Homes and accounts are the accounts' own: these tables name them by identifier only, and
forget what they hold of them when the accounts' events say they are gone.
"""

from pathlib import Path

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    SmallInteger,
    Table,
    Text,
    Uuid,
)

from ai_cloud.shared.database import Migrations, metadata

SCHEMA = "devices"

tables = metadata(SCHEMA)

# The registry: each device, in its home.
devices = Table(
    "devices",
    tables,
    # The identifier the device made itself: a device is in one home at most.
    Column("id", Uuid, primary_key=True),
    Column("home_id", Uuid, nullable=False),
    Column("create_time", DateTime(timezone=True), nullable=False),
    # What the device's latest hello on the cloud link said it is: its kind, its model and
    # the version of its software, all three or none until its first.
    Column("kind", Text),
    Column("model", Text),
    Column("software", Text),
    # A home's devices, in the order its lists show them.
    Index("ix_devices_home_id_id", "home_id", "id"),
)

# Each device's key, by its thumbprint, which its assertions name as their kid: a key is one
# device's at most. A device has one now; the cloud link lets it hold two while it changes
# them.
keys = Table(
    "keys",
    tables,
    Column("thumbprint", Text, primary_key=True),
    Column(
        "device_id",
        Uuid,
        ForeignKey(devices.c.id, ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    # The public key, as a JWK: kty, crv, x and y.
    Column("jwk", JSON, nullable=False),
    Column("create_time", DateTime(timezone=True), nullable=False),
)

# The challenges to register a device, until they are used. One that expired unused stays
# until the worker sweeps it, with the home or the account it was issued for.
challenges = Table(
    "challenges",
    tables,
    # The challenge's SHA-256: the challenge itself is never kept.
    Column("hash", LargeBinary, primary_key=True),
    Column("home_id", Uuid, nullable=False, index=True),
    Column("account_id", Uuid, nullable=False, index=True),
    Column("create_time", DateTime(timezone=True), nullable=False),
    Column("expire_time", DateTime(timezone=True), nullable=False),
)

# The first answers to registrations, by the account and the Idempotency-Key that sent them.
# A retry gets one for as long as the configuration's idempotency_retention says; sweeping
# older ones is the worker's job.
idempotency_keys = Table(
    "idempotency_keys",
    tables,
    Column("account_id", Uuid, primary_key=True),
    Column("key", Uuid, primary_key=True),
    # The SHA-256 of what the request asked: its method, its path and its body.
    Column("fingerprint", LargeBinary, nullable=False),
    # The home the request was about: the answer goes with it.
    Column("home_id", Uuid, nullable=False, index=True),
    # The device the answer shows, if it shows one: it goes with it too.
    Column("device_id", Uuid, ForeignKey(devices.c.id, ondelete="CASCADE"), index=True),
    Column("status", SmallInteger, nullable=False),
    Column("answer", JSON, nullable=False),
    Column("create_time", DateTime(timezone=True), nullable=False),
)

MIGRATIONS = Migrations(
    schema=SCHEMA, metadata=tables, directory=Path(__file__).parent / "migrations"
)
