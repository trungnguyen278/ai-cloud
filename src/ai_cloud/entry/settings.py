# SPDX-License-Identifier: MIT
"""The whole configuration, as every process role reads it.

Each role checks all of it when it starts, not only its own tables, so a mistake
anywhere stops every role at once rather than one of them later. Each module adds its
table here as it comes.
"""

from ai_cloud.entry.api import ApiSettings
from ai_cloud.shared.config import Settings
from ai_cloud.shared.logs import LoggingSettings


class CloudSettings(Settings):
    api: ApiSettings
    logging: LoggingSettings
