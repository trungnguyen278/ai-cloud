# SPDX-License-Identifier: MIT
"""The accounts' use cases: signing in, sessions, the account and its identities
(ai-platform ADR-0008, and its app API 1.0)."""

import base64
import binascii
import logging
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from ai_cloud.accounts.domain.errors import (
    IdentityConflict,
    IdentityLinked,
    IdentityTaken,
    IdTokenRefused,
    InvalidClient,
    InvalidGrant,
    InvalidPageToken,
    LastIdentity,
    NotFound,
    PreconditionFailed,
    RateLimited,
    RecentSignInRequired,
    TooManyIdentities,
    Unauthenticated,
)
from ai_cloud.accounts.domain.model import (
    MAX_IDENTITIES,
    Account,
    AccountChanges,
    Identity,
    Principal,
    Session,
    SessionPage,
    SignIn,
    Tokens,
    display_name_for,
    new_nonce,
    new_refresh_token,
    token_hash,
)
from ai_cloud.accounts.ports import DevelopmentIdTokens, IdTokens, Nonces, Store, Transaction
from ai_cloud.accounts.settings import AccountsSettings
from ai_cloud.shared.jose import SigningKeys, TokenError, TokenSettings
from ai_cloud.shared.protocols import protocol_schema
from ai_cloud.shared.rates import RateLimiter, address_subject

# The cloud's access tokens for the app: JWTs as RFC 9068 profiles them, with the
# audience that tells them from the tokens it issues to devices.
ACCESS_TOKEN_TYPE = "at+jwt"  # RFC 9068, 2.1
APP_AUDIENCE = "app"
_ACCESS_CLAIMS = ("iss", "sub", "aud", "exp", "iat", "jti", "client_id", "sid")

log = logging.getLogger(__name__)


class AccountsService:
    def __init__(
        self,
        *,
        settings: AccountsSettings,
        tokens: TokenSettings,
        keys: SigningKeys,
        store: Store,
        nonces: Nonces,
        nonce_rate: RateLimiter,
        id_tokens: IdTokens,
        development: DevelopmentIdTokens | None,
    ) -> None:
        self._settings = settings
        self._tokens = tokens
        self._keys = keys
        self._store = store
        self._nonces = nonces
        self._nonce_rate = nonce_rate
        self._id_tokens = id_tokens
        self._development = development
        self._idle = timedelta(seconds=settings.refresh_idle)
        self._email_schema = protocol_schema("app", "email")

    # Signing in.

    async def issue_nonce(self, address: str) -> tuple[str, int]:
        """A nonce for an ID token, and its lifetime in seconds. ``RateLimited`` when
        ``address`` has had its share."""
        rate = self._settings.nonce_rate
        wait = await self._nonce_rate.wait(address_subject(address, rate.ipv6_prefix))
        if wait > 0:
            raise RateLimited(wait)
        nonce = new_nonce()
        await self._nonces.issue(nonce, self._settings.nonce_lifetime)
        return nonce, self._settings.nonce_lifetime

    async def exchange(self, id_token: str, client_id: str, label: str | None) -> Tokens:
        """Sign in with a provider's ID token: its identity's account, created at its
        first sign-in, gets a new session."""
        self._check_client(client_id)
        sign_in = await self._sign_in(id_token)
        email = self._email(sign_in.email)
        now = _now()
        # A first sign-in can race another of the same identity once: the second then
        # finds the account the first made. One that races the account's deletion starts
        # again, and makes a new account.
        for attempt in range(2):
            try:
                async with self._store.transaction() as store:
                    owner = await store.identity(sign_in.provider, sign_in.subject)
                    if owner is None:
                        display_name = display_name_for(sign_in)
                        if display_name is None:
                            raise InvalidGrant("the ID token gives neither a name nor an email")
                        account_id = uuid.uuid7()
                        identity = Identity(
                            uuid.uuid7(), sign_in.provider, sign_in.subject, email, now
                        )
                        await store.add_account(
                            Account(
                                id=account_id,
                                display_name=display_name,
                                email=email,
                                languages=(),
                                identities=(identity,),
                                create_time=now,
                                version=1,
                            )
                        )
                    else:
                        account_id = owner.account_id
                        # The account first, as every change to it is locked: then a
                        # sign-in never deadlocks with the account's deletion.
                        if not await store.lock_account(account_id):
                            raise IdentityConflict("the account was deleted meanwhile")
                        if email is not None and email != owner.email:
                            await store.set_identity_email(owner.identity_id, email)
                    await store.end_idle_sessions(account_id, unused_since=now - self._idle)
                    session = Session(uuid.uuid7(), account_id, client_id, label, now, now)
                    refresh_token = new_refresh_token()
                    await store.add_session(session, token_hash(refresh_token))
                break
            except IdentityConflict:
                if attempt:
                    raise
        log.info(
            "signed in",
            extra={
                "account": str(account_id),
                "session": str(session.id),
                "provider": sign_in.provider,
                "new_account": owner is None,
            },
        )
        return self._tokens_for(session, refresh_token)

    async def refresh(self, refresh_token: str, client_id: str) -> Tokens:
        """The next tokens of a session. A refresh token works once: one presented again
        ends its session (RFC 9700, 4.14.2)."""
        self._check_client(client_id)
        presented = token_hash(refresh_token)
        now = _now()
        ended: str | None = None
        async with self._store.transaction() as store:
            session_id = await store.refresh_token_session(presented)
            # The session's lock, then its token's, in the order that ending a session
            # takes them. Of two requests with one token, the second waits here, then
            # finds the token used.
            session = None if session_id is None else await store.session(session_id, lock=True)
            if session is None:
                raise InvalidGrant("unknown refresh token")
            if session.client_id != client_id:
                raise InvalidGrant("the refresh token was issued to another client")
            if not await store.use_refresh_token(presented, now):
                await store.end_session(session.id, account_id=None)
                ended = "refresh token used twice: the session has ended"
            elif session.last_use_time <= now - self._idle:
                await store.end_session(session.id, account_id=None)
                ended = "the session was left unused too long"
            else:
                next_token = new_refresh_token()
                await store.continue_session(
                    session.id, token_hash(next_token), now, forget_before=now - self._idle
                )
        if ended is not None:
            log.warning(
                "a session has ended on refreshing",
                extra={"session": str(session.id), "reason": ended},
            )
            raise InvalidGrant(ended)
        return self._tokens_for(session, next_token)

    async def revoke(self, token: str, token_type_hint: str | None, client_id: str) -> None:
        """End the session of a refresh or access token (RFC 7009); a token that is
        neither, or another client's, ends nothing."""
        self._check_client(client_id)
        order = (
            (self._revoke_access, self._revoke_refresh)
            if token_type_hint == "access_token"
            else (self._revoke_refresh, self._revoke_access)
        )
        for revoke in order:
            if await revoke(token, client_id):
                return

    async def _revoke_refresh(self, token: str, client_id: str) -> bool:
        async with self._store.transaction() as store:
            session_id = await store.refresh_token_session(token_hash(token))
            session = None if session_id is None else await store.session(session_id, lock=True)
            if session is None or session.client_id != client_id:
                return False
            await store.end_session(session.id, account_id=None)
        log.info("signed out", extra={"session": str(session.id)})
        return True

    async def _revoke_access(self, token: str, client_id: str) -> bool:
        try:
            claims = self._access_claims(token)
        except Unauthenticated:
            return False
        if claims["client_id"] != client_id:
            return False
        principal = _principal(claims)
        async with self._store.transaction() as store:
            ended = await store.end_session(principal.session_id, account_id=principal.account_id)
        if ended:
            log.info("signed out", extra={"session": str(principal.session_id)})
        return ended

    async def authenticate(self, access_token: str) -> Principal:
        """The account and session an access token speaks for. ``Unauthenticated`` if it
        does not verify, or its session has ended: ending a session or deleting an
        account takes effect at once."""
        principal = _principal(self._access_claims(access_token))
        async with self._store.transaction() as store:
            session = await store.session(principal.session_id, lock=False)
        if session is None or session.account_id != principal.account_id:
            raise Unauthenticated("the session has ended")
        return principal

    # The account.

    async def account(self, principal: Principal) -> Account:
        async with self._store.transaction() as store:
            return await self._account(store, principal, lock=False)

    async def update_account(
        self, principal: Principal, changes: AccountChanges, if_match: str | None
    ) -> Account:
        """Change the account, if ``if_match`` names its ETag, or is ``None``."""
        async with self._store.transaction() as store:
            account = await self._account(store, principal, lock=True)
            if if_match is not None and not _etag_matches(if_match, account.etag):
                raise PreconditionFailed("the account changed since that ETag")
            updated = replace(
                account,
                display_name=changes.display_name or account.display_name,
                languages=account.languages if changes.languages is None else changes.languages,
                version=account.version + 1,
            )
            await store.save_account(updated)
        return updated

    async def delete_account(self, principal: Principal, id_token: str) -> None:
        """Delete the account, with everything that is the person's, once they have just
        signed in again with one of its identities."""
        try:
            sign_in = await self._sign_in(id_token)
        except InvalidGrant as error:
            raise RecentSignInRequired(str(error)) from None
        age = _now() - sign_in.issue_time
        if age > timedelta(seconds=self._settings.recent_sign_in):
            raise RecentSignInRequired("the ID token was issued too long ago")
        async with self._store.transaction() as store:
            account = await self._account(store, principal, lock=True)
            if not any(
                identity.provider == sign_in.provider and identity.subject == sign_in.subject
                for identity in account.identities
            ):
                raise RecentSignInRequired("the ID token is not one of the account's identities")
            await store.delete_account(account.id)
        log.info("account deleted", extra={"account": str(account.id)})

    # Identities.

    async def link_identity(self, principal: Principal, id_token: str) -> Identity:
        """Add the identity of a provider's ID token to the account."""
        try:
            sign_in = await self._sign_in(id_token)
        except InvalidGrant as error:
            raise IdTokenRefused(str(error)) from None
        now = _now()
        for attempt in range(2):
            try:
                async with self._store.transaction() as store:
                    account = await self._account(store, principal, lock=True)
                    owner = await store.identity(sign_in.provider, sign_in.subject)
                    if owner is not None and owner.account_id == account.id:
                        raise IdentityLinked("the identity is this account's already")
                    if owner is not None:
                        raise IdentityTaken("the identity is another account's")
                    if len(account.identities) >= MAX_IDENTITIES:
                        raise TooManyIdentities(f"an account has {MAX_IDENTITIES} at most")
                    identity = Identity(
                        uuid.uuid7(),
                        sign_in.provider,
                        sign_in.subject,
                        self._email(sign_in.email),
                        now,
                    )
                    await store.add_identity(account.id, identity)
                    await store.save_account(replace(account, version=account.version + 1))
                break
            except IdentityConflict:
                if attempt:
                    raise
        log.info(
            "identity linked",
            extra={"account": str(account.id), "identity": str(identity.id)},
        )
        return identity

    async def remove_identity(self, principal: Principal, identity_id: UUID) -> None:
        async with self._store.transaction() as store:
            account = await self._account(store, principal, lock=True)
            if identity_id not in {identity.id for identity in account.identities}:
                raise NotFound("no such identity in the account")
            if len(account.identities) == 1:
                raise LastIdentity("this is the account's only way to sign in")
            await store.delete_identity(identity_id)
            await store.save_account(replace(account, version=account.version + 1))
        log.info(
            "identity removed",
            extra={"account": str(account.id), "identity": str(identity_id)},
        )

    # Sessions.

    async def sessions(
        self, principal: Principal, page_size: int, page_token: str | None
    ) -> SessionPage:
        """A page of the account's sessions, newest first. ``InvalidPageToken`` for a
        page token this list did not give."""
        after = _page_after(page_token) if page_token else None
        async with self._store.transaction() as store:
            found = await store.sessions(
                principal.account_id,
                used_since=_now() - self._idle,
                after=after,
                limit=page_size + 1,
            )
        page = tuple(found[:page_size])
        more = len(found) > page_size
        return SessionPage(page, _page_token(page[-1].id) if more else "")

    async def end_session(self, principal: Principal, session_id: UUID) -> None:
        async with self._store.transaction() as store:
            ended = await store.end_session(session_id, account_id=principal.account_id)
        if not ended:
            raise NotFound("no such session in the account")
        log.info("session ended", extra={"session": str(session_id)})

    # Development.

    def development_id_token(self, nonce: str, display_name: str, email: str | None) -> str:
        """An ID token of the development provider. ``NotFound`` where it is off."""
        if self._development is None:
            raise NotFound("the development provider is off")
        return self._development.issue(nonce, display_name, email)

    # Helpers.

    def _email(self, email: str | None) -> str | None:
        """An email a provider gave, if the app API can carry it: the cloud keeps none
        that it could not show."""
        if email is None or self._email_schema.problem(email) is not None:
            return None
        return email

    def _check_client(self, client_id: str) -> None:
        if client_id not in self._settings.client_ids:
            raise InvalidClient("unknown client")

    async def _sign_in(self, id_token: str) -> SignIn:
        """A provider's ID token, verified, and its nonce used."""
        sign_in = await self._id_tokens.verify(id_token)
        if not await self._nonces.consume(sign_in.nonce):
            raise InvalidGrant("the ID token's nonce is unknown, used or expired")
        return sign_in

    async def _account(self, store: Transaction, principal: Principal, *, lock: bool) -> Account:
        account = await store.account(principal.account_id, lock=lock)
        if account is None:
            raise Unauthenticated("the account no longer exists")
        return account

    def _tokens_for(self, session: Session, refresh_token: str) -> Tokens:
        now = int(_now().timestamp())
        lifetime = self._settings.access_lifetime
        access_token = self._keys.sign(
            {
                "iss": self._tokens.issuer,
                "sub": str(session.account_id),
                "aud": APP_AUDIENCE,
                "client_id": session.client_id,
                "sid": str(session.id),
                "iat": now,
                "exp": now + lifetime,
                "jti": str(uuid.uuid7()),
            },
            typ=ACCESS_TOKEN_TYPE,
        )
        return Tokens(access_token, lifetime, refresh_token)

    def _access_claims(self, token: str) -> dict[str, object]:
        try:
            claims = self._keys.verify(
                token,
                typ=ACCESS_TOKEN_TYPE,
                issuer=self._tokens.issuer,
                audience=APP_AUDIENCE,
                leeway=self._tokens.leeway,
                require=_ACCESS_CLAIMS,
            )
        except TokenError as error:
            raise Unauthenticated(f"the access token is refused: {error}") from None
        return claims


def _now() -> datetime:
    return datetime.now(UTC)


def _principal(claims: dict[str, object]) -> Principal:
    try:
        return Principal(UUID(str(claims["sub"])), UUID(str(claims["sid"])))
    except ValueError:
        raise Unauthenticated("the access token is malformed") from None


def _etag_matches(if_match: str, etag: str) -> bool:
    """Whether ``If-Match`` names ``etag``: as written, the app API's ETags being
    compared as the account gives them; ``*`` matches any."""
    return any(candidate.strip() in ("*", etag) for candidate in if_match.split(","))


def _page_token(last: UUID) -> str:
    return base64.urlsafe_b64encode(last.bytes).rstrip(b"=").decode()


def _page_after(page_token: str) -> UUID:
    try:
        return UUID(bytes=base64.urlsafe_b64decode(page_token + "=" * (-len(page_token) % 4)))
    except binascii.Error, ValueError:
        raise InvalidPageToken("the page token is not one this list gave") from None
