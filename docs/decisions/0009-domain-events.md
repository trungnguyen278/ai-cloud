# 0009 — Domain events: an outbox in each module, relayed by the worker to Redis Streams

- **Status:** Accepted
- **Date:** 2026-11-05

## Context

- **Decided already:**
  - [ADR-0001](0001-architecture.md), decision 4: a module writes its event in the same transaction as its change, a transactional outbox, and `worker` delivers it through a queue. The [architecture](../architecture.md#growth) has Redis Streams for the queue, and NATS or Kafka behind the same port later.
  - ADR-0001, decision 5, and [ADR-0008](0008-data-and-tokens.md): PostgreSQL is the source of truth, and Redis holds only what can be lost, the queue included. Losing Redis must therefore lose no event.
  - The architecture: events carry a version and arrive at least once, so every consumer is idempotent.
- **The first events:** under [ai-platform ADR-0009](https://github.com/trungnguyen278/ai-platform/blob/main/docs/decisions/0009-people-in-a-home.md), the other modules learn from domain events that a person or a home is gone. `accounts` removes people, deletes homes and deletes accounts, and nothing tells the rest of the cloud yet. `devices`, which comes next, revokes a deleted home's devices.
- **What must hold:**
  - an event exists if and only if its change committed;
  - events about one thing keep their order;
  - every consumer gets every event at least once, whatever process stops, and even if Redis loses what it held;
  - an event that always fails stops nothing else;
  - nothing grows without bound.
- **How others do it** (retrieved 2026-11-05):

| Project | What it does |
|---|---|
| microservices.io | The transactional outbox: messages are sent "if and only if the database transaction commits". A relay polls the outbox (the polling publisher) or tails the transaction log. It may send a message twice, so consumers are idempotent, and record the IDs of what they processed. |
| Debezium 3.7 | It tails the log. Its outbox table has an `id`, an aggregate's type and id, the event's type and a payload. The aggregate's id keys the Kafka message, to keep each aggregate's order, and each aggregate type has a topic. |
| Eventuate's CDC | One reader is active at a time, by leader election. Its polling reader polls every 500 ms, for 1000 events at a time. |
| MassTransit | Its delivery service queries the outbox again at once while there is more, and waits when it is idle. It delivers one at a time by default, "to ensure message order". Its inbox keeps the IDs it received for a window, then deletes them. |
| NServiceBus | Its outbox is "expected to generate duplicate messages from time to time", and keeps its data longer than a message can be retried. |
| GitLab's EventStore | New events are CloudEvents, typed `com.gitlab.<category>.<type>` and named in the past tense. Their data is checked against a versioned schema and holds what subscribers need, and a schema change takes several rollouts. It warns that its jobs "may be dropped". |
| CloudEvents 1.0.2 | `source` with `id` is unique, `type` "SHOULD be prefixed with a reverse-DNS name", and there is a `subject`, and a `time` in RFC 3339. Its primer: a compatible change keeps the type, and an incompatible one changes it, with "a version number (`v1`, `v2`)". |
| Redis Streams (8.10) | Each consumer group has a list of pending entries with their delivery counts, and a consumer claims an entry that has waited. Past some number of deliveries, "put such messages in another stream": the dead letters. Explicit entry IDs serve IDs from "an SQL table". Persistence and asynchronous replication can lose writes, and with several consumers an item's messages "may be consumed out of order". |
| Kafka 4.4 | It keeps a topic's messages for 168 hours by default. |
| Sidekiq | It retries 25 times with an exponential backoff, over about 20 days, then moves the job to a dead set. |

- **Waking the relay:**
  - PostgreSQL sends a NOTIFY at commit, and only to the sessions listening then.
  - PgBouncer's transaction pooling, which the architecture's growth plans, never supports LISTEN, nor session-level advisory locks.
  - Recall.ai (2025) found that a transaction that has sent a NOTIFY takes a lock global to the database at its commit, which serialized their commits.
- **FastStream,** the Python framework for brokers, claims idle entries on Redis Streams. It leaves retry caps and dead letters to the application, and does not relay an outbox.

## Decision

1. **An outbox in each module, in its schema,** written through the module's transaction, its port:
   - its columns are `seq`, the order of writing; `id`, a UUIDv7; `type`; `subject`; `create_time`; `data`, in JSON; and `position`, its place in the stream once relayed;
   - `shared` declares it, and the module's migrations create it, so a module that moves to a database of its own takes its outbox along;
   - an event about a thing is written under that thing's lock, as every change to it is, so `seq` keeps their order.
2. **An event is a CloudEvent:**
   - its attributes are `id`, a UUIDv7; `source`, `/<module>`; `type`, `<module>.<thing>.<what happened>.v<major>`, such as `accounts.home.deleted.v1`; `subject`, the identifier of the thing; `time`, in UTC; and `data`, a JSON object in snake_case;
   - its data holds identifiers, and no personal data, which would outlive a deletion;
   - a change consumers can ignore, such as a new optional field, keeps the type. Any other makes a `.v2`, written beside the `.v1` until every consumer reads it;
   - the type has no reverse-DNS prefix. These events never leave the cloud, and its domain is a deployment's setting (dev log, 2026-11-04). An event for other repositories would be a protocol, in ai-platform.
3. **The relay, in `worker`, polls,** every `poll_interval` (0.5 s, as Eventuate's), and again at once while there is more. Each round, for each outbox:
   - **it numbers** the events committed since the last round, in the order of their `seq`, from a sequence. One relay at a time numbers an outbox, under a transaction-level advisory lock, so that each place follows the ones before it, even with several workers;
   - **it writes** what the outbox holds past the stream's last place to the module's stream, `<module>:events`. The entry's identifier is its place, `<position>-0`. Redis refuses an identifier that is not past the last one, so an event is in the stream once, and in order. The stream's last identifier says where to go on: after a crash, after Redis lost what it held, or with two relays at once;
   - a stream that holds places its outbox has not given yet, as after PostgreSQL is restored from a backup, is written again from the outbox. The relay checks it under the outbox's lock, before it gives any new place, since the stream would refuse an event at a place it already holds.
4. **The retention:** a relayed event stays in its outbox and its stream for `retention`, 7 days as Kafka's default: the longest a consumer may be away. An event not yet in its stream never goes. Dead letters are kept as long.
5. **Consumers are consumer groups:**
   - each is named `<module>.<what it does>`, and reads the streams of the types it handles, from the start of what they hold. It acknowledges an event once its handler returns, and at once one of a type it does not handle;
   - every worker runs every consumer, which handles one event at a time;
   - an event therefore arrives at least once, and handlers are idempotent. Deleting what is gone does nothing, and a handler whose effect would repeat records the event's id in its module's table, in its own transaction, as microservices.io's idempotent consumer does;
   - nor do handlers count on an order, since two workers, or a retry, reorder events.
6. **Retries and poison events:**
   - a handler that raises, or takes longer than `handler_timeout`, leaves its event pending. The event is claimed again after `retry_delay` (30 s), then twice as long each time, up to `retry_delay_max` (6 hours);
   - after `max_deliveries` (20, some 2.6 days), or at once for an entry that is not an event, the event goes to the dead letters of its consumer's module, `<module>:dead-events`, with the consumer's name and the reason. An error goes to the log, as Redis's documentation advises;
   - an event that a stopped consumer held is taken over once its delay has passed.
7. **The `worker` process:**
   - `ai-cloud worker` runs the relay and every module's consumers, and needs the database's and Redis's URLs only;
   - it stops on SIGINT or SIGTERM, or Ctrl+Break on Windows, once the relay's round and each handler are done. A consumer then leaves its group, unless it holds an event still to be tried again.
8. **The accounts' first events,** in `accounts/api.py`:
   - `accounts.member.removed.v1`, with `home_id`, `member_id` and `account_id` (`null` for a profile): a person left a home, was removed from it, or their account went;
   - `accounts.home.deleted.v1`, with `home_id`, for the home and everything it held, with no event for each of its people;
   - `accounts.account.deleted.v1`, with `account_id`, after the events of the homes it left and took along, in the same transaction.

## Alternatives considered

| Alternative | Why not |
|---|---|
| One outbox for the whole cloud | An event shares its change's transaction, and so its database: a module could no longer move to a database of its own (ADR-0001). |
| LISTEN/NOTIFY to wake the relay | Polling is needed anyway, for what was sent while no relay listened. LISTEN needs a session, which PgBouncer's transaction pooling never gives. Every transaction that notifies would also take a lock global to the database as it commits (Recall.ai, 2025). Half a second is soon enough for what follows a change. |
| Tailing the log, as Debezium | Logical decoding needs a replication slot, which keeps the WAL while its reader is down, and a service to run, for few events on one machine. It can come behind the same outbox should the volume call for it. |
| Ordering by transaction ids (`xid8`) | It reads in place, without gaps, but in the order in which transactions took their ids. A transaction that waited for a lock took its id early, so that order is not the order of one thing's changes. |
| Deleting an event once relayed, as Debezium can with Kafka | Redis may lose it before its consumers have read it. |
| Redis's own identifiers, with its idempotent XADD (Redis 8.6) | It remembers for a day at most, and the stream would not say what it lacks. Places from the outbox never repeat, and do say it. |
| Redis made durable (AOF, `appendfsync always`) | Redis would hold what cannot be lost, against ADR-0001, and replication still loses writes on a failover. |
| A fixed delay between tries, as `XAUTOCLAIM` has | An outage longer than the delay times the deliveries would put aside everything in flight. A growing delay rides it out. |
| A framework, such as FastStream | It claims entries, but leaves retry caps and dead letters to us, and has no relay. What it would save is a few Redis calls. |
| Dead letters in PostgreSQL | Redis's documented way is another stream. The event stays in its outbox for the retention, and the error log names it. |

## Consequences

- Positive:
  - an event stands or falls with its change;
  - a lost Redis, a crash, or two workers at once lose nothing, and repeat nothing in the stream;
  - modules see `Outbox`, `DomainEvent`, `Event` and `Consumer`, never Redis. An import contract keeps them off the queue, which NATS or Kafka can replace;
  - storage is bounded by the retention.
- Negative / accepted trade-offs:
  - an event waits up to half a second to be relayed;
  - a consumer away longer than the retention misses events;
  - handlers must be idempotent, and work in any order;
  - an event has two numbers, its `seq` and its place;
  - an event about a deleted account keeps its identifiers, and nothing else, for the retention: ADR-0006, on personal data and the law, should say so;
  - dead letters are lost with Redis. The error log has them, and the outbox for the retention.
- Follow-up:
  - `devices` consumes `accounts.home.deleted.v1` to revoke a home's devices (ai-platform ADR-0009, decision 7);
  - a shared helper that records an event's id, with the first handler whose effect would repeat;
  - replaying a dead letter, with `operations`' administration;
  - the worker's schedules: sweeping idle sessions, and old first answers;
  - the security events, a new sign-in or a refresh token used twice, with `notifications`;
  - the worker's container and its health check, with the production deployment.

## References

Retrieved 2026-11-05:

- microservices.io: [transactional outbox](https://microservices.io/patterns/data/transactional-outbox.html), [polling publisher](https://microservices.io/patterns/data/polling-publisher.html), [idempotent consumer](https://microservices.io/patterns/communication-style/idempotent-consumer.html).
- Debezium 3.7: [outbox event router](https://debezium.io/documentation/reference/stable/transformations/outbox-event-router.html).
- Eventuate: [CDC configuration](https://eventuate.io/docs/manual/eventuate-tram/latest/cdc-configuration.html).
- MassTransit: [transactional outbox](https://masstransit.massient.com/documentation/patterns/transactional-outbox), and [its configuration](https://masstransit.massient.com/documentation/configuration/middleware/outbox).
- NServiceBus: [outbox](https://docs.particular.net/nservicebus/outbox/).
- GitLab: [EventStore](https://docs.gitlab.com/ee/development/eventstore.html).
- CloudEvents: [the specification](https://github.com/cloudevents/spec/blob/main/cloudevents/spec.md) (1.0.2 is its latest release), [its primer](https://github.com/cloudevents/spec/blob/main/cloudevents/primer.md), and [its Kafka binding](https://github.com/cloudevents/spec/blob/main/cloudevents/bindings/kafka-protocol-binding.md).
- Redis: [streams](https://redis.io/docs/latest/develop/data-types/streams/), [XADD](https://redis.io/docs/latest/commands/xadd/), [XREADGROUP](https://redis.io/docs/latest/commands/xreadgroup/), and [idempotent message processing](https://redis.io/docs/latest/develop/data-types/streams/idempotency/). The image `redis:8-alpine` runs 8.10.3.
- Kafka 4.4: [broker configuration](https://kafka.apache.org/44/configuration/broker-configs/), `log.retention.hours`.
- Sidekiq: [error handling](https://github.com/sidekiq/sidekiq/wiki/Error-Handling).
- PostgreSQL 18: [NOTIFY](https://www.postgresql.org/docs/18/sql-notify.html), [sequences](https://www.postgresql.org/docs/18/functions-sequence.html), and [advisory locks](https://www.postgresql.org/docs/18/explicit-locking.html). PgBouncer: [features](https://www.pgbouncer.org/features.html).
- Recall.ai: [Postgres LISTEN/NOTIFY does not scale](https://www.recall.ai/blog/postgres-listen-notify-does-not-scale), 2025-07-01, updated 2026-05-08.
- FastStream: [claiming on Redis Streams](https://faststream.ag2.ai/latest/redis/streams/claiming/).
