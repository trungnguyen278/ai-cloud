# SPDX-License-Identifier: MIT
"""What the devices need from outside: their store, the assertions they have seen, and what
the accounts know of people."""

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from ai_cloud.devices.domain.model import (
    Access,
    Answer,
    Challenge,
    Device,
    DeviceKey,
    FirstAnswer,
    HeldKey,
)
from ai_cloud.shared.app_api import IdempotencyKey


class Transaction(Protocol):
    """Reads and writes that stand or fall together.

    A registration and a removal hold their home first, so that they happen one after the
    other, and the home's limit holds. Each transaction then deletes challenges, first
    answers, then devices with their keys, in that order, and the rows of each in the order
    of their keys, so that no two wait for each other in a circle.

    Forgetting a home, a person's place in it, or an account holds nothing: it forgets their
    challenges first. A registration that used one of them has it until it ends, and the
    rest waits for it; one that comes later finds its challenge gone. So nothing that a
    registration racing a deletion keeps outlives it.
    """

    async def hold_home(self, home_id: UUID) -> None:
        """Hold the home's devices until the transaction ends, waiting for whoever holds
        them now."""
        ...

    # Challenges.

    async def add_challenge(self, challenge_hash: bytes, challenge: Challenge) -> None: ...

    async def use_challenge(
        self, challenge_hash: bytes, home_id: UUID, account_id: UUID, now: datetime
    ) -> bool:
        """Forget the challenge whose hash this is, if it was issued for that home to that
        account and is unexpired; whether it was. Of two transactions, one only uses it."""
        ...

    async def forget_challenge(self, challenge_hash: bytes) -> None:
        """Forget the challenge whose hash this is, whatever it was issued for."""
        ...

    # Devices.

    async def device(self, device_id: UUID) -> Device | None:
        """The device with that identifier, in whatever home."""
        ...

    async def key_holder(self, thumbprint: str) -> UUID | None:
        """The identifier of the device that holds the key with that thumbprint."""
        ...

    async def device_key(self, thumbprint: str) -> HeldKey | None:
        """The key with that thumbprint, and the device that holds it."""
        ...

    async def devices(self, home_id: UUID, *, after: UUID | None, limit: int) -> list[Device]:
        """The home's devices, in the order of their identifiers, from the one after
        ``after``."""
        ...

    async def count_devices(self, home_id: UUID) -> int: ...

    async def add_device(self, device: Device, key: DeviceKey) -> bool:
        """Store the device with its key; whether it was stored: ``False`` if another
        transaction stored its identifier or its key first."""
        ...

    async def remove_device(self, home_id: UUID, device_id: UUID) -> bool:
        """Forget the device, if it is in that home, with the first answers that show it and
        its key; whether it was there."""
        ...

    # What goes with a home, a person's place in it, or an account.

    async def forget_home(self, home_id: UUID) -> int:
        """Forget what the devices keep for the home: its challenges, the first answers to
        requests about it, then its devices with their keys; how many devices it had."""
        ...

    async def forget_member(self, home_id: UUID, account_id: UUID) -> None:
        """Forget the challenges issued to the account for the home, then the first answers
        to its requests about the home."""
        ...

    async def forget_account(self, account_id: UUID) -> None:
        """Forget the challenges issued to the account, then the first answers to its
        requests."""
        ...

    # The first answers to requests with an Idempotency-Key.

    async def hold_idempotency_key(self, account_id: UUID, key: UUID) -> bool:
        """Hold the account's key until the transaction ends, without waiting; whether no
        other transaction holds it."""
        ...

    async def first_answer(
        self, account_id: UUID, key: UUID, *, since: datetime
    ) -> FirstAnswer | None:
        """What the key's first request was answered, if it came since ``since``."""
        ...

    async def add_first_answer(
        self,
        account_id: UUID,
        key: IdempotencyKey,
        home_id: UUID,
        device_id: UUID | None,
        answer: Answer,
        now: datetime,
    ) -> None:
        """Keep ``answer``, in place of an older answer under the same key. It goes with the
        home, and with ``device_id``, the device it shows, if it shows one."""
        ...


class Store(Protocol):
    """The devices' durable data."""

    def transaction(self) -> AbstractAsyncContextManager[Transaction]:
        """A transaction, committed when the block ends and rolled back if it raises."""
        ...


class Assertions(Protocol):
    """The assertions that devices have used, for as long as each is valid."""

    async def first_use(self, device_id: UUID, jti: str, until: datetime) -> bool:
        """Note that the device used the assertion with this ``jti``, until ``until``;
        whether it had not before. Of two requests with one assertion, one only gets
        ``True``."""
        ...


class People(Protocol):
    """What the accounts know of the people who use the app."""

    async def account(self, access_token: str) -> UUID:
        """The account an app's access token speaks for; ``Unauthenticated`` if the token
        does not verify, or its session has ended."""
        ...

    async def access(self, account_id: UUID, home_id: UUID) -> Access | None:
        """What the account may do with the home's devices; ``None`` if it is not in the
        home, or the home does not exist."""
        ...
