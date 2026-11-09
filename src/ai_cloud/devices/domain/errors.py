# SPDX-License-Identifier: MIT
"""What can go wrong with the devices' use cases. Messages say what happened, in a few words
for the logs and the error's description; never a token, a challenge or a registration."""


class DevicesError(Exception):
    """An error of the devices' use cases."""


class Unauthenticated(DevicesError):
    """An access token is missing, malformed, expired, or its session has ended."""


class RateLimited(DevicesError):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"over the rate: retry after {retry_after:.0f} s")
        self.retry_after = retry_after


class NotFound(DevicesError):
    """No such home among the account's, or no such device in the home."""


class RoleRequired(DevicesError):
    """The person's role in the home does not allow this."""


class RegistrationInvalid(DevicesError):
    """A device's registration does not verify, or its challenge is unknown, expired, used,
    or another account's or home's."""


class DeviceTaken(DevicesError):
    """The device is in another home, or its key or its identifier is another device's."""


class DeviceLimitReached(DevicesError):
    """The home has as many devices as it may have."""


class IdempotencyKeyBusy(DevicesError):
    """The first request with this Idempotency-Key still runs: the app tries again."""


class IdempotencyKeyReused(DevicesError):
    """This Idempotency-Key was used for another request."""


class UnknownDevice(DevicesError):
    """No device holds the key an assertion names: it was never registered, or was removed
    or revoked. The device offers set-up."""


class AssertionInvalid(DevicesError):
    """A device's assertion does not verify, or is used again, although the cloud holds its
    key: the device tries again later."""
