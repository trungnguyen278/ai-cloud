# SPDX-License-Identifier: MIT
"""The use cases of homes and the people in them: what each may do, how they join, how a
home changes hands and how it ends (ai-platform ADR-0009, and its app API 1.1).

A request that changes a home, or what is in it, holds the home until it ends, so that those
changes happen one after the other, and each reads what the one before it left. The lock
order is the account, if it needs one, then the home (the ports say why).
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from functools import cache
from importlib import resources
from typing import Any
from uuid import UUID

from ai_cloud.accounts.domain.errors import (
    HomeLimitReached,
    IdempotencyKeyBusy,
    IdempotencyKeyReused,
    InvitationInvalid,
    InvitationLimitReached,
    MemberLimitReached,
    NotFound,
    NotTransferable,
    OwnerCannotLeave,
    OwnsHomeWithMembers,
    PreconditionFailed,
    RateLimited,
    RoleNotChangeable,
    RoleRequired,
    Unauthenticated,
    UnknownTimeZone,
)
from ai_cloud.accounts.domain.events import HomeDeleted, MemberRemoved
from ai_cloud.accounts.domain.model import (
    Home,
    HomeChanges,
    HomeView,
    IdempotencyKey,
    Invitation,
    InvitationPreview,
    Member,
    MemberChanges,
    NewHome,
    NewInvitation,
    Page,
    Principal,
    Role,
    etag_matches,
    invitation_usable,
    may_change,
    may_manage,
    new_invitation_code,
    page_after,
    page_of,
    token_hash,
)
from ai_cloud.accounts.ports import Store, Transaction
from ai_cloud.accounts.service.common import SignIns, account_of, check_signs_in_to, now
from ai_cloud.accounts.settings import AccountsSettings
from ai_cloud.shared.rates import RateLimiter

log = logging.getLogger(__name__)


class HomesService:
    def __init__(
        self,
        *,
        settings: AccountsSettings,
        store: Store,
        sign_ins: SignIns,
        invitation_rate: RateLimiter,
    ) -> None:
        self._settings = settings
        self._store = store
        self._sign_ins = sign_ins
        self._invitation_rate = invitation_rate
        self._retention = timedelta(seconds=settings.idempotency_retention)
        self._invitation_lifetime = timedelta(seconds=settings.invitation_lifetime)
        self._time_zones = _time_zones()

    # Homes.

    async def homes(
        self, principal: Principal, page_size: int, page_token: str | None
    ) -> Page[HomeView]:
        """A page of the homes the account is in, oldest first."""
        after = page_after(page_token)
        async with self._store.transaction() as store:
            found = await store.homes_of(principal.account_id, after=after, limit=page_size + 1)
        return page_of(found, page_size, lambda view: view.home.id)

    async def create_home(
        self,
        principal: Principal,
        new: NewHome,
        key: IdempotencyKey,
        answer: Callable[[HomeView], dict[str, Any]],
    ) -> dict[str, Any]:
        """Create a home, whose owner is the person who asks, and give what ``answer`` makes
        of it: the first answer again, for a retry."""
        self._check_time_zone(new.time_zone)
        moment = now()
        async with self._store.transaction() as store:
            first = await self._first_answer(store, principal, key, moment)
            if first is not None:
                return first
            account = await account_of(store, principal, lock=False)
            await self._check_homes_of(store, account.id)
            home = Home(
                id=uuid.uuid7(),
                name=new.name,
                time_zone=new.time_zone,
                languages=new.languages,
                create_time=moment,
                version=1,
            )
            owner = Member(
                id=uuid.uuid7(),
                home_id=home.id,
                account_id=account.id,
                display_name=account.display_name,
                role=Role.OWNER,
                create_time=moment,
                version=1,
            )
            await store.add_home(home)
            await store.add_member(owner)
            body = answer(HomeView(home, owner))
            await store.add_first_answer(account.id, key, owner.id, body, moment)
        log.info("home created", extra={"home": str(home.id), "account": str(account.id)})
        return body

    async def home(self, principal: Principal, home_id: UUID) -> HomeView:
        async with self._store.transaction() as store:
            return await _in_home(store, home_id, principal, lock=False)

    async def update_home(
        self, principal: Principal, home_id: UUID, changes: HomeChanges, if_match: str | None
    ) -> HomeView:
        """Change the home, if ``if_match`` names its ETag, or is ``None``."""
        if changes.time_zone is not None:
            self._check_time_zone(changes.time_zone)
        async with self._store.transaction() as store:
            view = await _in_home(store, home_id, principal, lock=True)
            _check_may_manage(view.member)
            if if_match is not None and not etag_matches(if_match, view.home.etag):
                raise PreconditionFailed("the home changed since that ETag")
            home = view.home
            updated = replace(
                home,
                name=home.name if changes.name is None else changes.name,
                time_zone=home.time_zone if changes.time_zone is None else changes.time_zone,
                languages=home.languages if changes.languages is None else changes.languages,
                version=home.version + 1,
            )
            await store.save_home(updated)
        return HomeView(updated, view.member)

    async def transfer_home(
        self, principal: Principal, home_id: UUID, to_member_id: UUID, id_token: str
    ) -> HomeView:
        """Hand the home over, at once, once its owner has just signed in again: the person
        named owns it, and the old owner is an admin."""
        sign_in = await self._sign_ins.recent(id_token)
        async with self._store.transaction() as store:
            check_signs_in_to(await account_of(store, principal, lock=False), sign_in)
            view = await _in_home(store, home_id, principal, lock=True)
            _check_owner(view.member)
            target = await store.member(home_id, to_member_id)
            if target is None:
                raise NotFound("no such person in the home")
            if target.id == view.member.id or target.account_id is None:
                raise NotTransferable("the home goes to another person in it with an account")
            old_owner = replace(view.member, role=Role.ADMIN, version=view.member.version + 1)
            new_owner = replace(target, role=Role.OWNER, version=target.version + 1)
            updated = replace(view.home, version=view.home.version + 1)
            # In this order: a home never has two owners, not even within a transaction.
            await store.save_member(old_owner)
            await store.save_member(new_owner)
            await store.save_home(updated)
        log.info(
            "home handed over",
            extra={"home": str(home_id), "from_member": str(old_owner.id), "to": str(new_owner.id)},
        )
        return HomeView(updated, old_owner)

    async def delete_home(self, principal: Principal, home_id: UUID, id_token: str) -> None:
        """Delete the home, with its profiles and invitations, once its owner, its last
        person with an account, has just signed in again."""
        sign_in = await self._sign_ins.recent(id_token)
        async with self._store.transaction() as store:
            check_signs_in_to(await account_of(store, principal, lock=False), sign_in)
            view = await _in_home(store, home_id, principal, lock=True)
            _check_owner(view.member)
            if await store.has_other_accounts(home_id, view.member.id):
                raise OwnsHomeWithMembers("others with accounts are in the home")
            await store.delete_home(home_id)
            await store.emit(HomeDeleted(home_id))
        log.info("home deleted", extra={"home": str(home_id), "account": str(principal.account_id)})

    # The people in a home.

    async def members(
        self, principal: Principal, home_id: UUID, page_size: int, page_token: str | None
    ) -> Page[Member]:
        """A page of the home's people, oldest first, for anyone in it."""
        after = page_after(page_token)
        async with self._store.transaction() as store:
            await _in_home(store, home_id, principal, lock=False)
            found = await store.members(home_id, after=after, limit=page_size + 1)
        return page_of(found, page_size, lambda member: member.id)

    async def add_profile(
        self,
        principal: Principal,
        home_id: UUID,
        display_name: str,
        key: IdempotencyKey,
        answer: Callable[[Member], dict[str, Any]],
    ) -> dict[str, Any]:
        """Add a person who never signs in, and give what ``answer`` makes of them: the
        first answer again, for a retry."""
        moment = now()
        async with self._store.transaction() as store:
            first = await self._first_answer(store, principal, key, moment)
            view = await _in_home(store, home_id, principal, lock=True)
            if first is not None:
                return first
            _check_may_manage(view.member)
            await self._check_members_of(store, home_id)
            profile = Member(
                id=uuid.uuid7(),
                home_id=home_id,
                account_id=None,
                display_name=display_name,
                role=None,
                create_time=moment,
                version=1,
            )
            await store.add_member(profile)
            body = answer(profile)
            await store.add_first_answer(principal.account_id, key, profile.id, body, moment)
        log.info("profile added", extra={"home": str(home_id), "member": str(profile.id)})
        return body

    async def update_member(
        self,
        principal: Principal,
        home_id: UUID,
        member_id: UUID,
        changes: MemberChanges,
        if_match: str | None,
    ) -> Member:
        """Change a person, if ``if_match`` names their ETag, or is ``None``. Demoting an
        admin revokes the invitations they made."""
        async with self._store.transaction() as store:
            view = await _in_home(store, home_id, principal, lock=True)
            target = await store.member(home_id, member_id)
            if target is None:
                raise NotFound("no such person in the home")
            renames_another = changes.display_name is not None and target.id != view.member.id
            if renames_another and not may_change(view.member, target):
                raise RoleRequired("this role may not rename that person")
            if changes.role is not None:
                _check_owner(view.member)
                if target.account_id is None or target.role is Role.OWNER:
                    raise RoleNotChangeable("a profile has no role, and only handing over owns")
            if if_match is not None and not etag_matches(if_match, target.etag):
                raise PreconditionFailed("the person changed since that ETag")
            updated = replace(
                target,
                display_name=(
                    target.display_name if changes.display_name is None else changes.display_name
                ),
                role=target.role if changes.role is None else changes.role,
                version=target.version + 1,
            )
            await store.save_member(updated)
            if target.role is Role.ADMIN and updated.role is Role.MEMBER:
                await store.revoke_invitations_by(target.id)
        return updated

    async def remove_member(self, principal: Principal, home_id: UUID, member_id: UUID) -> None:
        """Remove a person, with the invitations they made: leaving, if it is the person who
        asks."""
        async with self._store.transaction() as store:
            view = await _in_home(store, home_id, principal, lock=True)
            target = await store.member(home_id, member_id)
            if target is None:
                raise NotFound("no such person in the home")
            if target.id == view.member.id:
                if target.role is Role.OWNER:
                    raise OwnerCannotLeave("hand the home over, or delete it")
            elif not may_change(view.member, target):
                raise RoleRequired("this role may not remove that person")
            await store.delete_member(target.id)
            await store.emit(MemberRemoved(home_id, target.id, target.account_id))
        log.info("person removed", extra={"home": str(home_id), "member": str(target.id)})

    # Invitations.

    async def create_invitation(self, principal: Principal, home_id: UUID) -> NewInvitation:
        """Make an invitation, whose code nobody can read again."""
        moment = now()
        async with self._store.transaction() as store:
            view = await _in_home(store, home_id, principal, lock=True)
            _check_may_manage(view.member)
            await store.forget_expired_invitations(home_id, moment)
            open_now = await store.count_open_invitations(home_id, moment)
            if open_now >= self._settings.invitations_per_home:
                raise InvitationLimitReached(
                    f"a home has {self._settings.invitations_per_home} open at most"
                )
            code = new_invitation_code()
            invitation = Invitation(
                id=uuid.uuid7(),
                home_id=home_id,
                invited_by=view.member.id,
                invited_by_name=view.member.display_name,
                create_time=moment,
                expire_time=moment + self._invitation_lifetime,
                used_by=None,
            )
            await store.add_invitation(invitation, token_hash(code))
        log.info("invitation made", extra={"home": str(home_id), "invitation": str(invitation.id)})
        return NewInvitation(invitation, code)

    async def invitations(
        self, principal: Principal, home_id: UUID, page_size: int, page_token: str | None
    ) -> Page[Invitation]:
        """A page of the home's open invitations, oldest first, without their codes."""
        after = page_after(page_token)
        moment = now()
        async with self._store.transaction() as store:
            view = await _in_home(store, home_id, principal, lock=False)
            _check_may_manage(view.member)
            found = await store.open_invitations(home_id, moment, after=after, limit=page_size + 1)
        return page_of(found, page_size, lambda invitation: invitation.id)

    async def revoke_invitation(
        self, principal: Principal, home_id: UUID, invitation_id: UUID
    ) -> None:
        async with self._store.transaction() as store:
            view = await _in_home(store, home_id, principal, lock=True)
            _check_may_manage(view.member)
            if not await store.revoke_invitation(home_id, invitation_id, now()):
                raise NotFound("no such open invitation in the home")
        log.info(
            "invitation revoked", extra={"home": str(home_id), "invitation": str(invitation_id)}
        )

    async def look_up_invitation(self, principal: Principal, code: str) -> InvitationPreview:
        """What a person sees before they agree to join."""
        await self._check_invitation_rate(principal)
        async with self._store.transaction() as store:
            invitation = await store.invitation_by_code(token_hash(code))
            if invitation is None:
                raise InvitationInvalid("this code cannot be used")
            asker = await store.member_of_account(invitation.home_id, principal.account_id)
            home = await store.home(invitation.home_id, lock=False)
            if home is None or not invitation_usable(invitation, asker, now()):
                raise InvitationInvalid("this code cannot be used")
        return InvitationPreview(home.name, invitation.invited_by_name, invitation.expire_time)

    async def accept_invitation(self, principal: Principal, code: str) -> tuple[HomeView, bool]:
        """Join the home of an invitation, as a member with the account's name; the home, and
        whether the person has just joined. A person already in it stays as they are, and an
        unused code stays unused."""
        await self._check_invitation_rate(principal)
        code_hash = token_hash(code)
        moment = now()
        async with self._store.transaction() as store:
            found = await store.invitation_by_code(code_hash)
            if found is None:
                raise InvitationInvalid("this code cannot be used")
            account = await account_of(store, principal, lock=True)
            home = await store.home(found.home_id, lock=True)
            # Read again now that the home is held: it may be used or revoked by now.
            invitation = await store.invitation_by_code(code_hash)
            if home is None or invitation is None:
                raise InvitationInvalid("this code cannot be used")
            member = await store.member_of_account(home.id, account.id)
            if not invitation_usable(invitation, member, moment):
                raise InvitationInvalid("this code cannot be used")
            if member is not None:
                return HomeView(home, member), False
            await self._check_homes_of(store, account.id)
            await self._check_members_of(store, home.id)
            member = Member(
                id=uuid.uuid7(),
                home_id=home.id,
                account_id=account.id,
                display_name=account.display_name,
                role=Role.MEMBER,
                create_time=moment,
                version=1,
            )
            await store.add_member(member)
            await store.use_invitation(invitation.id, member.id)
        log.info("joined a home", extra={"home": str(home.id), "member": str(member.id)})
        return HomeView(home, member), True

    # Helpers.

    async def _first_answer(
        self, store: Transaction, principal: Principal, key: IdempotencyKey, moment: datetime
    ) -> dict[str, Any] | None:
        """The first answer to a retry, or ``None`` for a first request. The key stays held,
        and the account too, whose row the answer will refer to."""
        account_id = principal.account_id
        if not await store.hold_idempotency_key(account_id, key.key):
            raise IdempotencyKeyBusy("the first request with this key still runs")
        if not await store.lock_account(account_id):
            raise Unauthenticated("the account no longer exists")
        first = await store.first_answer(account_id, key.key, since=moment - self._retention)
        if first is None:
            return None
        if first.fingerprint != key.fingerprint:
            raise IdempotencyKeyReused("this key was used for another request")
        return first.body

    async def _check_homes_of(self, store: Transaction, account_id: UUID) -> None:
        if await store.count_homes(account_id) >= self._settings.homes_per_account:
            raise HomeLimitReached(f"an account is in {self._settings.homes_per_account} at most")

    async def _check_members_of(self, store: Transaction, home_id: UUID) -> None:
        if await store.count_members(home_id) >= self._settings.members_per_home:
            raise MemberLimitReached(f"a home has {self._settings.members_per_home} at most")

    def _check_time_zone(self, name: str) -> None:
        if name not in self._time_zones:
            raise UnknownTimeZone("not a time zone of the IANA database")

    async def _check_invitation_rate(self, principal: Principal) -> None:
        wait = await self._invitation_rate.wait(str(principal.account_id))
        if wait > 0:
            raise RateLimited(wait)


async def remove_from_homes(store: Transaction, account_id: UUID) -> None:
    """A step of deleting an account, which is held: its person leaves every home it is in,
    and a home it owns goes with it. ``OwnsHomeWithMembers`` if others with accounts are in
    one of those, so that the deletion stops whole, events and all."""
    for home_id in await store.home_ids_of(account_id):
        if await store.home(home_id, lock=True) is None:
            continue
        member = await store.member_of_account(home_id, account_id)
        if member is None:
            continue
        if member.role is not Role.OWNER:
            await store.delete_member(member.id)
            await store.emit(MemberRemoved(home_id, member.id, account_id))
        elif await store.has_other_accounts(home_id, member.id):
            raise OwnsHomeWithMembers("others with accounts are in a home it owns")
        else:
            await store.delete_home(home_id)
            await store.emit(HomeDeleted(home_id))
            log.info("home deleted", extra={"home": str(home_id), "account": str(account_id)})


async def _in_home(
    store: Transaction, home_id: UUID, principal: Principal, *, lock: bool
) -> HomeView:
    """The home, as the person who asks sees it. ``NotFound`` if they are not in it, as if it
    did not exist (SEC-6). ``lock`` holds it until the transaction ends."""
    home = await store.home(home_id, lock=lock)
    member = None if home is None else await store.member_of_account(home_id, principal.account_id)
    if home is None or member is None:
        raise NotFound("no such home among the account's")
    return HomeView(home, member)


def _check_may_manage(member: Member) -> None:
    if not may_manage(member):
        raise RoleRequired("the owner or an admin may do this")


def _check_owner(member: Member) -> None:
    if member.role is not Role.OWNER:
        raise RoleRequired("only the owner may do this")


@cache
def _time_zones() -> frozenset[str]:
    """Every name in the IANA database, as the tzdata package lists it, which is where the
    standard library's ``available_timezones`` starts too. That function goes on to add what
    the host's own database holds, such as ``localtime``, so that a name good on one host may
    not be on the next. This reads a file: once, as a process puts its services together, and
    never while it serves."""
    names = resources.files("tzdata").joinpath("zones").read_text(encoding="utf-8")
    return frozenset(names.split())
