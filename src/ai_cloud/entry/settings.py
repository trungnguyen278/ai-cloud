# SPDX-License-Identifier: MIT
"""The whole configuration, as every process role reads it.

Each role checks all of it when it starts, not only its own tables, so a mistake
anywhere stops every role at once rather than one of them later. Each module adds its
table here as it comes.
"""

from typing import Annotated, Self

from pydantic import Field, model_validator

from ai_cloud.accounts.api import AccountsSettings
from ai_cloud.shared.config import Settings
from ai_cloud.shared.database import DatabaseSettings
from ai_cloud.shared.events import EventsSettings
from ai_cloud.shared.jose import TokenSettings
from ai_cloud.shared.logs import LoggingSettings
from ai_cloud.shared.redis import RedisSettings


class ApiSettings(Settings):
    # The address the process listens on.
    host: str
    # Its TCP port.
    port: Annotated[int, Field(ge=1, le=65535)]
    # Whether it logs each request.
    access_log: bool
    # The addresses or networks of the proxies whose X-Forwarded-For names the client
    # (uvicorn's forwarded_allow_ips): the reverse proxy in front of the api. Rate limits
    # count by the address they give.
    forwarded_allow_ips: list[str]
    # Bytes a request's body may have at most (SEC-15).
    max_body_size: Annotated[int, Field(ge=1)]


class CloudSettings(Settings):
    api: ApiSettings
    logging: LoggingSettings
    database: DatabaseSettings
    redis: RedisSettings
    tokens: TokenSettings
    events: EventsSettings
    accounts: AccountsSettings

    @model_validator(mode="after")
    def _reads_end_before_redis_gives_up(self) -> Self:
        # Redis's client would cut a longer read short, as if Redis had failed.
        if self.events.read_wait >= self.redis.timeout:
            raise ValueError("events.read_wait must be less than redis.timeout")
        return self
