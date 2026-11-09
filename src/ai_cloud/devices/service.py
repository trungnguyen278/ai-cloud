# SPDX-License-Identifier: MIT
"""The devices' use cases: challenges, registrations, a home's devices, and what goes when a
home or an account does (ai-platform ADR-0012, and its app API 1.4).

A registration and a removal hold their home, as the ports say. A registration uses its
challenge first of all, and keeps its answer in the same transaction, whatever it is: so a
challenge works once, a retry gets the first answer, and nothing is tried twice on one
challenge.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from ai_cloud.accounts.api import AccountDeleted, HomeDeleted, MemberRemoved
from ai_cloud.devices.domain import registration
from ai_cloud.devices.domain.errors import (
    DeviceLimitReached,
    DevicesError,
    DeviceTaken,
    IdempotencyKeyBusy,
    IdempotencyKeyReused,
    NotFound,
    RateLimited,
    RegistrationInvalid,
    RoleRequired,
)
from ai_cloud.devices.domain.model import (
    Access,
    Answer,
    Binding,
    Challenge,
    Device,
    IssuedChallenge,
    Registered,
    binding,
    challenge_hash,
    new_challenge,
)
from ai_cloud.devices.ports import People, Store, Transaction
from ai_cloud.devices.settings import DevicesSettings
from ai_cloud.shared.app_api import AppApiSettings, IdempotencyKey, Page, page_of
from ai_cloud.shared.events import Event
from ai_cloud.shared.rates import RateLimiter

log = logging.getLogger(__name__)

# What a registration ends in, once its challenge is used: the device, or why not.
type Outcome = Registered | RegistrationInvalid | DeviceTaken | DeviceLimitReached


def now() -> datetime:
    return datetime.now(UTC)


class DevicesService:
    def __init__(
        self,
        *,
        settings: DevicesSettings,
        app_api: AppApiSettings,
        store: Store,
        people: People,
        challenge_rate: RateLimiter,
    ) -> None:
        self._settings = settings
        self._store = store
        self._people = people
        self._challenge_rate = challenge_rate
        self._challenge_lifetime = timedelta(seconds=settings.challenge_lifetime)
        self._retention = timedelta(seconds=app_api.idempotency_retention)

    async def authenticate(self, access_token: str) -> UUID:
        """The account an app's access token speaks for."""
        return await self._people.account(access_token)

    # Adding a device.

    async def issue_challenge(self, account_id: UUID, home_id: UUID) -> IssuedChallenge:
        """A challenge to register one device into the home, for the owner or an admin."""
        wait = await self._challenge_rate.wait(str(account_id))
        if wait > 0:
            raise RateLimited(wait)
        await self._check_manages(account_id, home_id)
        challenge = new_challenge()
        hashed = challenge_hash(challenge)
        moment = now()
        issued = Challenge(home_id, account_id, moment, moment + self._challenge_lifetime)
        async with self._store.transaction() as store:
            await store.add_challenge(hashed, issued)
        # Checked again, now that it is stored. A deletion of the home, of the person's place
        # in it, or of the account, that came since the first check finds the challenge, or
        # comes before this one, which forgets it: none outlives what it was issued for.
        try:
            await self._check_manages(account_id, home_id)
        except DevicesError:
            async with self._store.transaction() as store:
                await store.forget_challenge(hashed)
            raise
        log.info(
            "device challenge issued", extra={"home": str(home_id), "account": str(account_id)}
        )
        return IssuedChallenge(challenge, self._settings.server, self._settings.challenge_lifetime)

    async def register(
        self,
        account_id: UUID,
        home_id: UUID,
        signed: str,
        key: IdempotencyKey,
        answer: Callable[[Outcome], Answer],
    ) -> Answer:
        """Register the device that signed ``signed`` into the home, and give what ``answer``
        makes of the outcome: the first answer again, for a retry."""
        access = await self._people.access(account_id, home_id)
        if access is None:
            raise NotFound("no such home among the account's")
        moment = now()
        async with self._store.transaction() as store:
            first = await self._first_answer(store, account_id, key, moment)
            if first is not None:
                return first
            if access is not Access.MANAGE:
                raise RoleRequired("the owner or an admin adds devices")
            challenge = registration.challenge_of(signed)
            if challenge is None:
                raise RegistrationInvalid("it names no challenge")
            await store.hold_home(home_id)
            used = await store.use_challenge(challenge_hash(challenge), home_id, account_id, moment)
            if not used:
                raise RegistrationInvalid("its challenge is unknown, expired, used, or another's")
            # The challenge is used, whatever follows: the answer is kept with it.
            outcome: Outcome
            try:
                outcome = await self._bind(store, home_id, signed, moment)
            except (RegistrationInvalid, DeviceTaken, DeviceLimitReached) as refusal:
                outcome = refusal
            given = answer(outcome)
            shown = outcome.device.id if isinstance(outcome, Registered) else None
            await store.add_first_answer(account_id, key, home_id, shown, given, moment)
        _log_registration(home_id, outcome)
        return given

    # A home's devices.

    async def devices(
        self, account_id: UUID, home_id: UUID, page_size: int, after: UUID | None
    ) -> Page[Device]:
        """A page of the home's devices, in the order of their identifiers, from the one
        after ``after``, for anyone in it."""
        if await self._people.access(account_id, home_id) is None:
            raise NotFound("no such home among the account's")
        async with self._store.transaction() as store:
            found = await store.devices(home_id, after=after, limit=page_size + 1)
        return page_of(found, page_size, lambda device: device.id)

    async def device(self, account_id: UUID, home_id: UUID, device_id: UUID) -> Device:
        """One of the home's devices, for anyone in it."""
        if await self._people.access(account_id, home_id) is None:
            raise NotFound("no such home among the account's")
        async with self._store.transaction() as store:
            device = await store.device(device_id)
        if device is None or device.home_id != home_id:
            raise NotFound("no such device in the home")
        return device

    async def remove(self, account_id: UUID, home_id: UUID, device_id: UUID) -> None:
        """Remove a device from the home, which revokes it, for the owner or an admin: the
        cloud forgets its key."""
        await self._check_manages(account_id, home_id)
        async with self._store.transaction() as store:
            await store.hold_home(home_id)
            if not await store.remove_device(home_id, device_id):
                raise NotFound("no such device in the home")
        log.info("device removed", extra={"home": str(home_id), "device": str(device_id)})

    # Helpers.

    async def _bind(
        self, store: Transaction, home_id: UUID, signed: str, moment: datetime
    ) -> Registered:
        """The device that signed the registration, in the home: as it was, or new there."""
        verified = registration.verify(signed, audience=self._settings.server)
        holder = await store.device(verified.device_id)
        placed = binding(home_id, holder, await store.key_holder(verified.key.thumbprint))
        if placed is Binding.TAKEN:
            raise DeviceTaken("the cloud holds its identifier or its key elsewhere")
        if placed is Binding.SAME:
            assert holder is not None
            return Registered(holder, new=False)
        # The limit counts new devices only: a full home takes back one that lost its state.
        if await store.count_devices(home_id) >= self._settings.devices_per_home:
            raise DeviceLimitReached(f"a home has {self._settings.devices_per_home} at most")
        device = Device(id=verified.device_id, home_id=home_id, create_time=moment)
        if not await store.add_device(device, verified.key):
            raise DeviceTaken("another registration took its identifier or its key meanwhile")
        return Registered(device, new=True)

    async def _first_answer(
        self, store: Transaction, account_id: UUID, key: IdempotencyKey, moment: datetime
    ) -> Answer | None:
        """The first answer to a retry, or ``None`` for a first request. The key stays held
        until the transaction ends."""
        if not await store.hold_idempotency_key(account_id, key.key):
            raise IdempotencyKeyBusy("the first request with this key still runs")
        first = await store.first_answer(account_id, key.key, since=moment - self._retention)
        if first is None:
            return None
        if first.fingerprint != key.fingerprint:
            raise IdempotencyKeyReused("this key was used for another request")
        return first.answer

    async def _check_manages(self, account_id: UUID, home_id: UUID) -> None:
        access = await self._people.access(account_id, home_id)
        if access is None:
            raise NotFound("no such home among the account's")
        if access is not Access.MANAGE:
            raise RoleRequired("the owner or an admin adds and removes devices")


class Forgetting:
    """What the devices forget when the accounts say that a home, a person's place in it, or
    an account is gone (docs/decisions/0009-domain-events.md). Each handler is idempotent:
    forgetting what is gone does nothing."""

    def __init__(self, store: Store) -> None:
        self._store = store

    async def home_deleted(self, event: Event) -> None:
        """A deleted home's devices are revoked, and its challenges and first answers go."""
        home_id = HomeDeleted.from_data(event.data).home_id
        async with self._store.transaction() as store:
            revoked = await store.forget_home(home_id)
        if revoked:
            log.info(
                "a deleted home's devices are revoked",
                extra={"home": str(home_id), "devices": revoked},
            )

    async def member_removed(self, event: Event) -> None:
        """What a person who left a home asked for there goes: their challenges and first
        answers. The devices they added are the home's, and stay. A profile asked for
        nothing."""
        removed = MemberRemoved.from_data(event.data)
        if removed.account_id is None:
            return
        async with self._store.transaction() as store:
            await store.forget_member(removed.home_id, removed.account_id)

    async def account_deleted(self, event: Event) -> None:
        """A deleted account's challenges and first answers go."""
        account_id = AccountDeleted.from_data(event.data).account_id
        async with self._store.transaction() as store:
            await store.forget_account(account_id)


def _log_registration(home_id: UUID, outcome: Outcome) -> None:
    about = {"home": str(home_id)}
    match outcome:
        case Registered(device=device, new=new):
            message = "device registered" if new else "device registered again"
            log.info(message, extra={**about, "device": str(device.id)})
        case DevicesError():
            log.info("device registration refused", extra={**about, "reason": str(outcome)})
