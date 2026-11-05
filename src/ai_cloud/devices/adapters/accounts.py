# SPDX-License-Identifier: MIT
"""What the accounts know of people, through their api (docs/architecture.md, How modules
talk)."""

from uuid import UUID

from ai_cloud.accounts.api import Accounts
from ai_cloud.accounts.api import Unauthenticated as AccountUnauthenticated
from ai_cloud.devices.domain.errors import Unauthenticated
from ai_cloud.devices.domain.model import Access


class AccountsPeople:
    def __init__(self, accounts: Accounts) -> None:
        self._accounts = accounts

    async def account(self, access_token: str) -> UUID:
        try:
            principal = await self._accounts.authenticate(access_token)
        except AccountUnauthenticated as error:
            raise Unauthenticated(str(error)) from None
        return principal.account_id

    async def access(self, account_id: UUID, home_id: UUID) -> Access | None:
        membership = await self._accounts.membership(account_id, home_id)
        if membership is None:
            return None
        # Those who manage a home add and remove its devices; everyone in it sees them.
        return Access.MANAGE if membership.manages else Access.SEE
