# SPDX-License-Identifier: MIT
"""How the accounts' errors become the app API's problems (platform/protocols/app/README.md,
Errors). The conventions every module's routes share are in ``shared/app_api.py``."""

from collections.abc import Awaitable

from ai_cloud.accounts.domain import errors
from ai_cloud.shared.app_api import StatusProblems, TypedProblems, over_the_rate, problem_of
from ai_cloud.shared.problems import Problem

# The problems the app acts on (the app API, Errors), with their titles.
_TYPED: TypedProblems = {
    errors.OwnsHomeWithMembers: ("owns_home_with_members", 409, "Owns a home with members"),
    errors.IdentityTaken: ("identity_taken", 409, "Identity taken"),
    errors.IdentityLinked: ("identity_linked", 409, "Identity linked"),
    errors.LastIdentity: ("last_identity", 409, "Last identity"),
    errors.RecentSignInRequired: ("recent_sign_in_required", 403, "Recent sign-in required"),
    errors.RoleRequired: ("role_required", 403, "Role required"),
    errors.OwnerCannotLeave: ("owner_cannot_leave", 409, "Owner cannot leave"),
    errors.InvitationInvalid: ("invitation_invalid", 404, "Invitation invalid"),
    errors.HomeLimitReached: ("home_limit_reached", 409, "Home limit reached"),
    errors.MemberLimitReached: ("member_limit_reached", 409, "Member limit reached"),
    errors.InvitationLimitReached: ("invitation_limit_reached", 409, "Invitation limit reached"),
}
# Any other problem is about:blank.
_STATUSES: StatusProblems = {
    errors.Unauthenticated: 401,
    errors.IdTokenRefused: 403,
    errors.NotFound: 404,
    errors.UnknownTimeZone: 400,
    errors.TooManyIdentities: 409,
    errors.NotTransferable: 409,
    errors.RoleNotChangeable: 409,
    errors.IdempotencyKeyBusy: 409,
    errors.PreconditionFailed: 412,
    errors.IdempotencyKeyReused: 422,
    errors.ProviderUnavailable: 503,
}


async def call[T](work: Awaitable[T]) -> T:
    """``work``'s result; its errors as the API answers them."""
    try:
        return await work
    except errors.AccountsError as error:
        raise _problem(error) from None


def _problem(error: errors.AccountsError) -> Problem:
    if isinstance(error, errors.RateLimited):
        return over_the_rate(error.retry_after, str(error))
    return problem_of(error, _TYPED, _STATUSES)
