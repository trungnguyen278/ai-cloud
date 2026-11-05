# SPDX-License-Identifier: MIT
"""The events' table of the configuration, ``[events]`` (docs/decisions/0009-domain-events.md)."""

from typing import Annotated, Self

from pydantic import Field, model_validator

from ai_cloud.shared.config import Settings

# Beyond this many doublings a delay is past any retry_delay_max: an event's deliveries are
# counted by Redis, in 64 bits.
_MAX_DOUBLINGS = 63


class EventsSettings(Settings):
    # Seconds between two looks at the outboxes while they are empty.
    poll_interval: Annotated[float, Field(gt=0)]
    # Events relayed, or swept, at a time.
    batch_size: Annotated[int, Field(ge=1)]
    # Seconds an event stays in its outbox and its stream once relayed, and a dead letter is
    # kept.
    retention: Annotated[float, Field(gt=0)]
    # Seconds between two sweeps of what is past the retention.
    sweep_interval: Annotated[float, Field(gt=0)]
    # Seconds the relay and the consumers wait after PostgreSQL or Redis failed them.
    reconnect_delay: Annotated[float, Field(gt=0)]
    # Seconds a consumer waits for a new event in one read.
    read_wait: Annotated[float, Field(gt=0)]
    # Seconds a handler may take before it counts as failed.
    handler_timeout: Annotated[float, Field(gt=0)]
    # Seconds before the first retry of an event whose handler failed; each next one waits
    # twice as long, up to retry_delay_max.
    retry_delay: Annotated[float, Field(gt=0)]
    retry_delay_max: Annotated[float, Field(gt=0)]
    # Deliveries an event gets before it is put aside as a dead letter.
    max_deliveries: Annotated[int, Field(ge=1)]

    def backoff(self, deliveries: int) -> float:
        """Seconds an event delivered ``deliveries`` times, the last of them in vain, waits
        before it is tried again."""
        doublings = min(max(deliveries - 1, 0), _MAX_DOUBLINGS)
        return min(self.retry_delay * 2.0**doublings, self.retry_delay_max)

    def retry_span(self) -> float:
        """Seconds an event may spend from its first delivery to its last: every handler
        overrunning, and every wait in between."""
        waits = sum(self.backoff(deliveries) for deliveries in range(1, self.max_deliveries))
        return waits + self.max_deliveries * self.handler_timeout

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.handler_timeout >= self.retry_delay:
            # Otherwise a retry could start while the handler before it still runs.
            raise ValueError("handler_timeout must be less than retry_delay")
        if self.retry_delay_max < self.retry_delay:
            raise ValueError("retry_delay_max must be at least retry_delay")
        if self.retry_span() >= self.retention:
            # Otherwise the stream could let an event go while it is still being retried.
            raise ValueError("retention must be longer than an event's retries take")
        return self
