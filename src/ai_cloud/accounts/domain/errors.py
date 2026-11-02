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
    """No such identity or session in the account."""


class PreconditionFailed(AccountsError):
    """The account changed since the ETag the request named."""


class IdentityTaken(AccountsError):
    """The identity belongs to another account."""


class IdentityLinked(AccountsError):
    """The identity is already this account's."""


class LastIdentity(AccountsError):
    """The identity is the account's last way to sign in."""


class TooManyIdentities(AccountsError):
    """The account has as many identities as it may."""


class RecentSignInRequired(AccountsError):
    """Deleting the account needs a fresh sign-in with one of its identities."""


class IdentityConflict(AccountsError):
    """Storing an identity that another transaction stored first."""
