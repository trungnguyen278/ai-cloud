# SPDX-License-Identifier: MIT
"""The app API's entity tags (ai-platform ADR-0004; the app API, Changing it): a resource's
``etag`` names its version, and ``If-Match`` makes a change only if it still has it. Every
module that serves a resource that changes tags it so."""


def weak_etag(version: int) -> str:
    """The ETag of a resource at ``version``."""
    return f'W/"{version}"'


def etag_matches(if_match: str, etag: str) -> bool:
    """Whether ``If-Match`` names ``etag``: as written, the app API's ETags being compared
    as the cloud gives them; ``*`` matches any."""
    return any(candidate.strip() in ("*", etag) for candidate in if_match.split(","))
