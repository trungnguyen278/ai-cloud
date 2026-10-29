# SPDX-License-Identifier: MIT
"""The cloud side of the project's AI devices (docs/architecture.md).

- ``shared``: what every module needs, such as the configuration and the logs.
- ``entry``: the process roles, ``api`` first.
"""

from importlib.metadata import PackageNotFoundError, version


def software_version() -> str:
    """The version of the installed package, or ``"(not installed)"``."""
    try:
        return version("ai-cloud")
    except PackageNotFoundError:
        return "(not installed)"
