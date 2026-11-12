# SPDX-License-Identifier: MIT
"""The devices' use cases: challenges, registrations, a home's devices and their names, the
devices' access tokens, and what goes when a home or an account does (ai-platform ADR-0012 and
ADR-0013, its app API 1.5, and the cloud link 3.0). A device removed, or gone with its home,
is hinted at, so that its control channel ends (docs/decisions/0011-control-channels.md).

A registration and a removal hold their home, as the ports say. A registration uses its
challenge first of all, and keeps its answer in the same transaction, whatever it is: so a
challenge works once, a retry gets the first answer, and nothing is tried twice on one
challenge.

A rename publishes no domain event, as the accounts' renames do not: no other module acts on
a device's name, and an event's data holds no personal data
(docs/decisions/0009-domain-events.md).
"""

import logging
import uuid
from collections.abc import Callable, Collection
from datetime import UTC, datetime, timedelta
from uuid import UUID

from ai_cloud.accounts.api import AccountDeleted, HomeDeleted, MemberRemoved
from ai_cloud.devices.domain import assertion, registration
from ai_cloud.devices.domain.errors import (
    AssertionInvalid,
    DeviceLimitReached,
    DevicesError,
    DeviceTaken,
    IdempotencyKeyBusy,
    IdempotencyKeyReused,
    NotFound,
    PreconditionFailed,
    RateLimited,
    RegistrationInvalid,
    RoleRequired,
    Unauthenticated,
    UnknownDevice,
)
from ai_cloud.devices.domain.model import (
    Access,
    Answer,
    Binding,
    Challenge,
    Device,
    DeviceToken,
    IssuedChallenge,
    Registered,
    Shown,
    TokenHolder,
    binding,
    challenge_hash,
    identifier,
    named_again,
    new_challenge,
    renamed,
)
from ai_cloud.devices.ports import Assertions, Hints, People, Presence, Store, Transaction
from ai_cloud.devices.settings import DevicesSettings
from ai_cloud.shared.app_api import AppApiSettings, IdempotencyKey, Page, page_of
from ai_cloud.shared.etags import etag_matches
from ai_cloud.shared.events import Event
from ai_cloud.shared.jose import SigningKeys, TokenError, TokenSettings
from ai_cloud.shared.rates import RateLimiter, address_subject

log = logging.getLogger(__name__)

# What a registration ends in, once its challenge is used: the device, or why not.
type Outcome = Registered | RegistrationInvalid | DeviceTaken | DeviceLimitReached

# The devices' access tokens (RFC 9068): for the device's side of the cloud, its control
# channel. The app's have the audience app, so that neither passes for the other.
ACCESS_TOKEN_TYPE = "at+jwt"  # RFC 9068, 2.1
DEVICE_AUDIENCE = "device"
# What a device's access token must hold: those the cloud gives it, which the control channel
# reads.
_ACCESS_CLAIMS = ("iss", "sub", "aud", "client_id", "iat", "exp")


def now() -> datetime:
    return datetime.now(UTC)


async def hint(hints: Hints, device_ids: Collection[UUID]) -> None:
    """Hint that these devices' standing changed, so that the replicas that hold their
    control channels check them. A hint that cannot be sent is lost as one in flight may be:
    the replicas' next check finds what it would have said."""
    try:
        await hints.publish(device_ids)
    except Exception:
        log.warning(
            "a hint about devices could not be sent: their next check will find it",
            exc_info=True,
        )


class DevicesService:
    def __init__(
        self,
        *,
        settings: DevicesSettings,
        app_api: AppApiSettings,
        store: Store,
        people: People,
        presence: Presence,
        hints: Hints,
        challenge_rate: RateLimiter,
    ) -> None:
        self._settings = settings
        self._store = store
        self._people = people
        self._presence = presence
        self._hints = hints
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
        await self._check_manages(account_id, home_id, "adds devices")
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
            await self._check_manages(account_id, home_id, "adds devices")
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
        name: str | None,
        key: IdempotencyKey,
        answer: Callable[[Outcome], Answer],
    ) -> Answer:
        """Register the device that signed ``signed`` into the home, with the name it gives
        itself, if the app sent one; and give what ``answer`` makes of the outcome: the first
        answer again, for a retry."""
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
                outcome = await self._bind(store, home_id, signed, name, moment)
            except (RegistrationInvalid, DeviceTaken, DeviceLimitReached) as refusal:
                outcome = refusal
            given = answer(outcome)
            shown = outcome.device.device.id if isinstance(outcome, Registered) else None
            await store.add_first_answer(account_id, key, home_id, shown, given, moment)
        _log_registration(home_id, outcome)
        return given

    # A home's devices.

    async def devices(
        self, account_id: UUID, home_id: UUID, page_size: int, after: UUID | None
    ) -> Page[Shown]:
        """A page of the home's devices, in the order of their identifiers, from the one
        after ``after``, for anyone in it."""
        if await self._people.access(account_id, home_id) is None:
            raise NotFound("no such home among the account's")
        async with self._store.transaction() as store:
            found = await store.devices(home_id, after=after, limit=page_size + 1)
        page = page_of(found, page_size, lambda device: device.id)
        connected = await self._presence.connected([device.id for device in page.items])
        shown = tuple(Shown(device, device.id in connected) for device in page.items)
        return Page(shown, page.next_page_token)

    async def device(self, account_id: UUID, home_id: UUID, device_id: UUID) -> Shown:
        """One of the home's devices, for anyone in it."""
        if await self._people.access(account_id, home_id) is None:
            raise NotFound("no such home among the account's")
        async with self._store.transaction() as store:
            device = await store.device(device_id)
        if device is None or device.home_id != home_id:
            raise NotFound("no such device in the home")
        return await self._shown(device)

    async def rename(
        self, account_id: UUID, home_id: UUID, device_id: UUID, name: str, if_match: str | None
    ) -> Shown:
        """Rename one of the home's devices, for the owner or an admin, if ``if_match`` names
        its ETag, or is ``None``."""
        await self._check_manages(account_id, home_id, "renames devices")
        async with self._store.transaction() as store:
            device = await store.hold_device(device_id)
            if device is None or device.home_id != home_id:
                raise NotFound("no such device in the home")
            if if_match is not None and not etag_matches(if_match, device.etag):
                raise PreconditionFailed("the device changed since that ETag")
            device = renamed(device, name)
            await store.save_name(device)
        log.info("device renamed", extra={"home": str(home_id), "device": str(device_id)})
        return await self._shown(device)

    async def remove(self, account_id: UUID, home_id: UUID, device_id: UUID) -> None:
        """Remove a device from the home, which revokes it, for the owner or an admin: the
        cloud forgets its key."""
        await self._check_manages(account_id, home_id, "removes devices")
        async with self._store.transaction() as store:
            await store.hold_home(home_id)
            if not await store.remove_device(home_id, device_id):
                raise NotFound("no such device in the home")
        log.info("device removed", extra={"home": str(home_id), "device": str(device_id)})
        # Its control channel ends with bye revoked.
        await hint(self._hints, [device_id])

    # Helpers.

    async def _bind(
        self, store: Transaction, home_id: UUID, signed: str, name: str | None, moment: datetime
    ) -> Registered:
        """The device that signed the registration, in the home: as it was, or new there with
        ``name``."""
        verified = registration.verify(signed, audience=self._settings.server)
        holder = await store.device(verified.device_id)
        placed = binding(home_id, holder, await store.key_holder(verified.key.thumbprint))
        if placed is Binding.TAKEN:
            raise DeviceTaken("the cloud holds its identifier or its key elsewhere")
        if placed is Binding.SAME:
            # Held, for a rename may race this: removing it, or its home, waits for the challenge.
            held = await store.hold_device(verified.device_id)
            assert held is not None
            if (named := named_again(held, name)) is not None:
                held = named
                await store.save_name(held)
            return Registered(await self._shown(held), new=False)
        # The limit counts new devices only: a full home takes back one that lost its state.
        if await store.count_devices(home_id) >= self._settings.devices_per_home:
            raise DeviceLimitReached(f"a home has {self._settings.devices_per_home} at most")
        device = Device(
            id=verified.device_id,
            home_id=home_id,
            create_time=moment,
            description=None,
            name=name,
            version=1,
        )
        if not await store.add_device(device, verified.key):
            raise DeviceTaken("another registration took its identifier or its key meanwhile")
        # Its key was nobody's: no connection of it can be open.
        return Registered(Shown(device, connected=False), new=True)

    async def _shown(self, device: Device) -> Shown:
        return Shown(device, device.id in await self._presence.connected([device.id]))

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

    async def _check_manages(self, account_id: UUID, home_id: UUID, does: str) -> None:
        """That the account may manage the home's devices: the owner or an admin ``does``."""
        access = await self._people.access(account_id, home_id)
        if access is None:
            raise NotFound("no such home among the account's")
        if access is not Access.MANAGE:
            raise RoleRequired(f"the owner or an admin {does}")


class DeviceTokens:
    """The token endpoint's use case: a device proves who it is with an assertion that its key
    signs, and gets an access token for its control channel (the cloud link's Tokens from the
    server; RFC 7523, 2.2)."""

    def __init__(
        self,
        *,
        settings: DevicesSettings,
        tokens: TokenSettings,
        keys: SigningKeys,
        store: Store,
        assertions: Assertions,
        token_rate: RateLimiter,
    ) -> None:
        self._settings = settings
        self._tokens = tokens
        self._keys = keys
        self._store = store
        self._assertions = assertions
        self._token_rate = token_rate
        # The token endpoint's URL, as the device makes it from the server it was registered
        # with: without a trailing slash, then the path.
        self._audience = settings.server.rstrip("/") + assertion.TOKEN_PATH
        self._leeway = timedelta(seconds=tokens.leeway)

    async def issue(self, address: str, signed: str, client_id: str | None) -> DeviceToken:
        """An access token for the device whose key signed ``signed``, asked for from
        ``address``. ``client_id``, if the request has one, names the same device (RFC 7521,
        4.2). ``UnknownDevice`` if the cloud holds no such key; ``AssertionInvalid`` if the
        assertion does not verify, or was used before."""
        subject = address_subject(address, self._settings.token_rate.ipv6_prefix)
        wait = await self._token_rate.wait(subject)
        if wait > 0:
            raise RateLimited(wait)
        kid = assertion.key_of(signed)
        async with self._store.transaction() as store:
            held = await store.device_key(kid)
        if held is None:
            raise UnknownDevice("no device holds this key")
        asserted = assertion.verify(
            signed,
            held.jwk,
            held.device_id,
            audience=self._audience,
            leeway=self._tokens.leeway,
        )
        if client_id is not None and identifier(client_id) != held.device_id:
            raise AssertionInvalid("client_id names another device")
        # Once it has verified, so that only the device itself can use an assertion up.
        until = asserted.expire_time + self._leeway
        if not await self._assertions.first_use(held.device_id, asserted.jti, until):
            raise AssertionInvalid("this assertion was used before")
        issued = int(now().timestamp())
        lifetime = self._settings.access_lifetime
        device = str(held.device_id)
        token = self._keys.sign(
            {
                "iss": self._tokens.issuer,
                "sub": device,
                "aud": DEVICE_AUDIENCE,
                "client_id": device,
                "iat": issued,
                "exp": issued + lifetime,
                "jti": str(uuid.uuid7()),
            },
            typ=ACCESS_TOKEN_TYPE,
        )
        log.info("device token issued", extra={"device": device})
        return DeviceToken(token, lifetime)

    def authenticate(self, access_token: str) -> TokenHolder:
        """The device that an access token from this endpoint speaks for, and when it was
        issued; ``Unauthenticated`` if it does not verify, or has expired."""
        try:
            claims = self._keys.verify(
                access_token,
                typ=ACCESS_TOKEN_TYPE,
                issuer=self._tokens.issuer,
                audience=DEVICE_AUDIENCE,
                leeway=self._tokens.leeway,
                require=_ACCESS_CLAIMS,
            )
        except TokenError as error:
            raise Unauthenticated(f"the access token is refused: {error}") from None
        device_id = identifier(claims["sub"])
        issued = claims["iat"]
        # The cloud signed these claims: anything else in them is another kind of token.
        if device_id is None or identifier(claims["client_id"]) != device_id:
            raise Unauthenticated("the access token names no device")
        if isinstance(issued, bool) or not isinstance(issued, int):
            raise Unauthenticated("the access token's iat is not a number of seconds")
        return TokenHolder(device_id, issued)


class Forgetting:
    """What the devices forget when the accounts say that a home, a person's place in it, or
    an account is gone (docs/decisions/0009-domain-events.md). Each handler is idempotent:
    forgetting what is gone does nothing."""

    def __init__(self, store: Store, hints: Hints) -> None:
        self._store = store
        self._hints = hints

    async def home_deleted(self, event: Event) -> None:
        """A deleted home's devices are revoked, and its challenges and first answers go.
        Their control channels end with bye revoked."""
        home_id = HomeDeleted.from_data(event.data).home_id
        async with self._store.transaction() as store:
            revoked = await store.forget_home(home_id)
        if revoked:
            log.info(
                "a deleted home's devices are revoked",
                extra={"home": str(home_id), "devices": len(revoked)},
            )
            await hint(self._hints, revoked)

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
            log.info(message, extra={**about, "device": str(device.device.id)})
        case DevicesError():
            log.info("device registration refused", extra={**about, "reason": str(outcome)})
