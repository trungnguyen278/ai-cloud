# SPDX-License-Identifier: MIT
"""The app API's entity tags (ai-platform ADR-0004; the app API 1.7, Requests, ``etag``): a
resource's ``etag`` names its version, and a change that brings it is made only if the
resource still has it. Every module that serves a resource that changes tags it so."""

import enum
from dataclasses import dataclass


def weak_etag(version: int) -> str:
    """The ETag of a resource at ``version``."""
    return f'W/"{version}"'


class Broken(enum.Enum):
    """The condition of a change that the resource no longer meets."""

    # The If-Match of apps of 1.1 to 1.6: 412.
    IF_MATCH = enum.auto()
    # The etag in the change's body, since 1.7: 409 etag_mismatch.
    ETAG = enum.auto()


@dataclass(frozen=True)
class Condition:
    """What a change is made on: the ``etag`` that its body brings, compared as written,
    and the ``If-Match`` that apps of 1.1 to 1.6 send instead, a list of etags or ``*``,
    which matches any. A change that brings neither is made whatever the resource's etag,
    and one that brings both meets each."""

    etag: str | None = None
    if_match: str | None = None

    def broken(self, etag: str) -> Broken | None:
        """The condition that the resource, at ``etag``, breaks, if any. ``If-Match`` comes
        first, as RFC 9110 has a server evaluate a request's preconditions before it
        processes the request's content (13.2.1), which holds the body's etag."""
        if self.if_match is not None and not _names(self.if_match, etag):
            return Broken.IF_MATCH
        if self.etag is not None and self.etag != etag:
            return Broken.ETAG
        return None


def _names(if_match: str, etag: str) -> bool:
    """Whether ``If-Match`` names ``etag``: as written, the app API's etags being compared
    as the cloud gives them (the app API, Requests, ``etag``); ``*`` matches any."""
    return any(candidate.strip() in ("*", etag) for candidate in if_match.split(","))
