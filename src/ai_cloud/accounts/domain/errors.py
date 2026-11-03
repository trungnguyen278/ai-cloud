# SPDX-License-Identifier: MIT
"""What can go wrong with the accounts' use cases. Messages say what happened, in a few
words for the logs and the error's description; never a token, an email or a name."""


class AccountsError(Exception):
    """An error of the accounts' use cases."""


class InvalidClient(AccountsError):
    """The client ID is not one of the apps'."""


class InvalidGrant(AccountsError):
    """An ID token, its nonce or a refresh token failed: the app signs in again."""


class IdTokenRefused(AccountsError):
    """An ID token given to link an identity failed."""


class ProviderUnavailable(AccountsError):
    """A provider's keys cannot be had, so its ID tokens cannot be verified for now."""


class Unauthenticated(AccountsError):
    """An access token is missing, malformed, expired, or its session has ended."""


class RateLimited(AccountsError):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"over the rate: retry after {retry_after:.0f} s")
        self.retry_after = retry_after


class InvalidPageToken(AccountsError):
    """A page token that the list did not give."""


class NotFound(AccountsError):
    """No such item in the account, or in a home it is in."""


class PreconditionFailed(AccountsError):
    """What the request changes has changed since the ETag it named."""


class IdentityTaken(AccountsError):
    """The identity belongs to another account."""


class IdentityLinked(AccountsError):
    """The identity is already this account's."""


class LastIdentity(AccountsError):
    """The identity is the account's last way to sign in."""


class TooManyIdentities(AccountsError):
    """The account has as many identities as it may."""


class RecentSignInRequired(AccountsError):
    """Deleting the account or a home, or handing a home over, needs a fresh sign-in with
    one of the account's identities."""


class IdentityConflict(AccountsError):
    """Storing an identity that another transaction stored first."""


class RoleRequired(AccountsError):
    """The person's role in the home does not allow this."""


class OwnerCannotLeave(AccountsError):
    """The owner leaves only by handing the home over or deleting it."""


class OwnsHomeWithMembers(AccountsError):
    """The account owns a home with other people with accounts, so it cannot delete itself
    or that home yet."""


class NotTransferable(AccountsError):
    """A home is handed over only to another person in it with an account."""


class RoleNotChangeable(AccountsError):
    """A role is changed only on a person with an account who is not the owner."""


class InvitationInvalid(AccountsError):
    """An invitation's code is unknown, expired, revoked, or used by someone else."""


class HomeLimitReached(AccountsError):
    """The account is in as many homes as it may be."""


class MemberLimitReached(AccountsError):
    """The home has as many people as it may have."""


class InvitationLimitReached(AccountsError):
    """The home has as many open invitations as it may have."""


class UnknownTimeZone(AccountsError):
    """A time zone that the IANA database does not name."""


class IdempotencyKeyBusy(AccountsError):
    """The first request with this Idempotency-Key still runs: the app tries again."""


class IdempotencyKeyReused(AccountsError):
    """This Idempotency-Key was used for another request."""
