# SPDX-License-Identifier: MIT
"""Secrets: from the environment, or from a file the deployment's secret store mounts.

A secret such as ``AI_CLOUD_DATABASE_URL`` is that variable's value, or the content of
the file that ``AI_CLOUD_DATABASE_URL_FILE`` names, as Docker and Kubernetes mount their
secrets. Never both: which one counts would be a guess. Each process role reads the
secrets it needs, and no more. A problem names the variable, never the value.
"""

import os
from collections.abc import Mapping
from pathlib import Path

from ai_cloud.shared.config import ConfigError

FILE_SUFFIX = "_FILE"


def read_secret(name: str, environ: Mapping[str, str] | None = None) -> str:
    """The secret called ``name``; ``ConfigError`` if it is missing, empty or set twice."""
    environ = os.environ if environ is None else environ
    value = environ.get(name)
    file = environ.get(name + FILE_SUFFIX)
    if value is not None and file is not None:
        raise ConfigError(f"{name} and {name}{FILE_SUFFIX} are both set: set one of them")
    if file is not None:
        try:
            value = Path(file).read_text(encoding="utf-8")
        except OSError as error:
            raise ConfigError(f"{name}{FILE_SUFFIX}: {error.strerror or 'cannot read'}") from None
        except UnicodeDecodeError:
            raise ConfigError(f"{name}{FILE_SUFFIX}: the file is not UTF-8") from None
        # A file written by hand or by an editor ends with a line break.
        value = value.rstrip("\r\n")
    if not value:
        raise ConfigError(f"{name} is not set, nor {name}{FILE_SUFFIX}")
    return value
