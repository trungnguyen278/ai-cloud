# SPDX-License-Identifier: MIT
"""What the accounts' use cases share: the time, the account of a request, and the ID tokens
people sign in with."""

from datetime import UTC, datetime, timedelta

from ai_cloud.accounts.domain.errors import InvalidGrant, RecentSignInRequired, Unauthenticated
from ai_cloud.accounts.domain.model import Account, Principal, SignIn
from ai_cloud.accounts.ports import IdTokens, Nonces, Transaction


def now() -> datetime:
    return datetime.now(UTC)


async def account_of(store: Transaction, principal: Principal, *, lock: bool) -> Account:
    """The account a request speaks for; ``lock`` holds it until the transaction ends."""
    account = await store.account(principal.account_id, lock=lock)
    if account is None:
        raise Unauthenticated("the account no longer exists")
    return account


class SignIns:
    """The ID tokens people sign in with."""

    def __init__(self, *, nonces: Nonces, id_tokens: IdTokens, recent_sign_in: int) -> None:
        self._nonces = nonces
        self._id_tokens = id_tokens
        self._recent = timedelta(seconds=recent_sign_in)

    async def verify(self, id_token: str) -> SignIn:
        """A provider's ID token, verified, and its nonce used."""
        sign_in = await self._id_tokens.verify(id_token)
        if not await self._nonces.consume(sign_in.nonce):
            raise InvalidGrant("the ID token's nonce is unknown, used or expired")
        return sign_in

    async def recent(self, id_token: str) -> SignIn:
        """A provider's ID token, for what a phone left unlocked must not be enough for.
        ``RecentSignInRequired`` if it fails, or was issued too long ago."""
        try:
            sign_in = await self.verify(id_token)
        except InvalidGrant as error:
            raise RecentSignInRequired(str(error)) from None
        if now() - sign_in.issue_time > self._recent:
            raise RecentSignInRequired("the ID token was issued too long ago")
        return sign_in


def check_signs_in_to(account: Account, sign_in: SignIn) -> None:
    """``RecentSignInRequired`` unless ``sign_in`` is one of the account's identities."""
    if not any(
        identity.provider == sign_in.provider and identity.subject == sign_in.subject
        for identity in account.identities
    ):
        raise RecentSignInRequired("the ID token is not one of the account's identities")
