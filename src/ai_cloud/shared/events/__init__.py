# SPDX-License-Identifier: MIT
"""Domain events: how a module tells the others what happened to what it owns
(docs/decisions/0009-domain-events.md).

A module declares its ``Outbox`` beside its tables, and writes each event into it in the
transaction of the change it records. The worker's relay takes them from there to the
module's stream, and hands them to each ``Consumer`` that handles their type. A module sees
nothing of the queue: what it writes is a ``DomainEvent``, and what it handles an ``Event``.
"""

from ai_cloud.shared.events.model import Consumer, DomainEvent, Event, Handler
from ai_cloud.shared.events.outbox import Outbox
from ai_cloud.shared.events.settings import EventsSettings

__all__ = ["Consumer", "DomainEvent", "Event", "EventsSettings", "Handler", "Outbox"]
