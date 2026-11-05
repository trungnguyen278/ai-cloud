# SPDX-License-Identifier: MIT
"""What the accounts tell the other modules: who left a home, which home and which account
went (ai-platform ADR-0009, decision 7; docs/decisions/0009-domain-events.md, decision 8).

Each is written in the transaction of its change. Its data holds identifiers only: the
names, emails and languages of a person who left are no one else's to keep. A consumer deletes
what it holds of that person, that home or that account.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Self
from uuid import UUID


@dataclass(frozen=True)
class MemberRemoved:
    """A person is no longer in a home: they left, someone removed them, or their account
    was deleted. What is theirs in that home goes; their account and their other homes
    stay. A profile, who has no account, is removed the same way."""

    TYPE: ClassVar[str] = "accounts.member.removed.v1"

    home_id: UUID
    member_id: UUID
    # None for a profile.
    account_id: UUID | None

    @property
    def subject(self) -> str:
        return str(self.member_id)

    def data(self) -> dict[str, Any]:
        return {
            "home_id": str(self.home_id),
            "member_id": str(self.member_id),
            "account_id": None if self.account_id is None else str(self.account_id),
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> Self:
        """The event that ``data``, as ``data()`` wrote it, holds."""
        account_id = data["account_id"]
        return cls(
            home_id=UUID(data["home_id"]),
            member_id=UUID(data["member_id"]),
            account_id=None if account_id is None else UUID(account_id),
        )


@dataclass(frozen=True)
class HomeDeleted:
    """A home is gone, and everything it held with it: its people, profiles included, and
    its invitations. No event comes for each of those people."""

    TYPE: ClassVar[str] = "accounts.home.deleted.v1"

    home_id: UUID

    @property
    def subject(self) -> str:
        return str(self.home_id)

    def data(self) -> dict[str, Any]:
        return {"home_id": str(self.home_id)}

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> Self:
        """The event that ``data``, as ``data()`` wrote it, holds."""
        return cls(home_id=UUID(data["home_id"]))


@dataclass(frozen=True)
class AccountDeleted:
    """An account is gone, with everything that was the person's (PRIV-4). The events for
    the homes it left, and those it took along, come first, in the same transaction."""

    TYPE: ClassVar[str] = "accounts.account.deleted.v1"

    account_id: UUID

    @property
    def subject(self) -> str:
        return str(self.account_id)

    def data(self) -> dict[str, Any]:
        return {"account_id": str(self.account_id)}

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> Self:
        """The event that ``data``, as ``data()`` wrote it, holds."""
        return cls(account_id=UUID(data["account_id"]))


type AccountsEvent = MemberRemoved | HomeDeleted | AccountDeleted
