# 0011 — The devices' control channels: sessions in the replica that holds them, presence and hints in Redis, and checks against the registry

- **Status:** Accepted
- **Date:** 2026-11-11

## Context

- **What is decided already:**
  - the cloud link 3.0 (ai-platform's `protocols/cloud`): a device keeps one WebSocket to the cloud, `/device/v3/link`, opened with its access token. A newer connection of the device replaces the older one, which the cloud ends with `bye` `replaced`; when its registration is revoked, the cloud ends its session with `bye` `revoked` (SEC-3). Pings keep it, and before the cloud closes it on purpose, it sends `reconnect`. A session can resume after a short loss, for `resume_window`;
  - the app API 1.4: a device's `connected` "says whether the device's control channel is open now", and removing it, or deleting its home, revokes it;
  - [the architecture](../architecture.md): `api` grows by replicas that keep no state, Redis holds presence among what may be lost, and the control channel is a port;
  - [ADR-0009](0009-domain-events.md) and [ADR-0010](0010-device-registrations.md): a home's deletion reaches the `devices` module through the worker, some time after the home went, and only the registry decides who gets a token.
- **What must hold:**
  - a device has one connection open: a newer one ends the older, whichever replicas hold them;
  - a device removed, or gone with its home, is told so wherever it is connected, and connects no more; a token issued before its removal opens nothing, should it be registered again;
  - every replica reads `connected`, which is true only while a connection is open, also once a replica has stopped without closing its own;
  - none of it depends on a message arriving: what replicas tell each other may be lost.
- **What the tools do** (retrieved 2026-11-11):
  - **uvicorn 0.54.0,** in its source: `Server.shutdown` closes every WebSocket itself, with code 1012, before the application's lifespan ends; ASGI delivers whole messages, and a message over `ws_max_size` fails its connection with 1009.
  - **Redis publish and subscribe** "exhibits at-most-once message delivery semantics": "If the subscriber is unable to handle the message (for example, due to an error or a network disconnect) the message is forever lost." Socket.IO has an adapter on Redis Streams for that reason: it can "properly handle any temporary disconnection to the Redis server and resume the stream" "without losing any packets".
  - **Redis's lock pattern** releases a key "with a script that only removes the key if the value matches". Redis 8.4 adds `SET … IFEQ` and `DELEX`, which redis-py 8.1 marks as experimental.
- **How others do it** (retrieved 2026-11-11):
  - **AWS IoT** disconnects a client whose client ID a newer connection uses (`DUPLICATE_CLIENTID`), and warns that its connection events "might be sent out of order. You might receive duplicate messages."
  - **Centrifugo** keeps presence in Redis for `presence_ttl`, 60 s by default: "How long presence is considered valid if not confirmed by active client connection."
  - **Kubernetes**' controllers are "control loops that watch the state of your cluster, then make or request changes where needed": they bring what is toward what should be, again and again, rather than trusting each change to be seen once.

## Decision

1. **A session lives in the replica that holds its connection.** The channel is a port, `Channel`, with a WebSocket adapter; the session does not know WebSockets. Three tasks carry it, as the robot's carry its side: one reads, numbers each message and answers pings at once; one handles what is queued; one writes, in order.
   - **What the cloud takes from a device is bounded before it is read:** `rate` messages a second, in bursts of `burst`, pings included, so that no message costs the cloud work beyond the rate. Up to `backlog` wait their turn, past which what comes is dropped, and the device told so once (`rate_limited`).
   - **What it sends is bounded by time,** as what it receives is: a connection to which nothing could be sent for two ping intervals closes, as a silent one does, so that what waits is bounded by the rate. A count of what waits would be passed by one burst of answers.
   - **Nothing a device sends raises out of its session.** Its frames are read as `shared/protocols.py`'s `read_json` reads the app API's bodies: JSON that nests deeper than `[devices.link] max_json_depth`, which RFC 8259 lets a reader limit (section 9) and which the app API's bodies limit alike with `[app_api] max_json_depth`, numbers past a double, which I-JSON leaves out (RFC 7493, 2.2), and lone surrogates are not JSON to the cloud. Before the hello, they end the session `no_hello`; after it, they are ignored, as a message without a number is, for nothing in them can be answered. A type that is no text is numbered, and fails the schema with `invalid_message`. A failure of the cloud's own ends the session with `bye` `error` and close 1011, the server's unexpected condition (RFC 6455, 7.4.1), and is logged; what a session logs once has fixed keys.
2. **The handshake checks the token, then the registry.** The access token must be the cloud's own for a device (`at+jwt`, its audience `device`, unexpired, `sub` and `client_id` the same device). The registry must hold the device under a registration no newer than the token, give or take the tokens' leeway: a device removed, or removed and registered again, gets 401, gets a new token, and offers set-up if the token endpoint knows it no more. A replica that is going away answers 503, also when it went away while the registry was read; a session that begins after all is closed with 1012 before its hello, as those it held then are, with nothing sent.
3. **The hello is checked twice against the registry.** The device's hello comes first; what it says the device is, its kind, model and software, is kept by an update that succeeds only under the registration the connection opened with. The session was already held by then, hearing hints: a removal that commits after this update hints after it, and the hint reaches the session.
4. **Presence, in Redis:** `devices:presence:<device>` holds the identifier of the device's newest connection, for `presence_ttl`, as Centrifugo's presence lasts.
   - The newest connection sets it, and learns the one it replaces (`SET … GET`).
   - Its replica keeps it with a script: kept while it still names that connection, noted again if it lapsed, as after Redis lost it, and reported if it names another.
   - Closing forgets it with a script, only if it still names that connection.
   - `connected` reads it, for a page of devices at once.
5. **Hints, on one channel of Redis's publish and subscribe,** `devices:hints`, each naming a device whose standing may have changed: a newer connection, a removal, a home's deletion. The worker's consumer sends the last. Every replica hears every hint, and checks the connections it holds of that device. A hint carries no verdict, only a device to check: a hint lost, late or repeated changes when a session ends, never whether.
6. **Checks, every `check_interval`:** each replica checks all its connections, with one query of the registry and one pipeline of Redis.
   - A device that the registry no longer holds under its connection's registration gets `bye` `revoked`.
   - A connection that the presence no longer names gets `bye` `replaced`.
   - So a lost hint delays a `bye` by `check_interval` at most. The checks keep the presence too.
7. **Going away:** the api's server asks the devices to refuse new connections and send `reconnect` on each session, then waits up to `close_timeout`, before uvicorn closes what is left. The stop is bounded: uvicorn waits `[api] shutdown_timeout`, its `timeout_graceful_shutdown`, for its connections to close, then cancels what is left, such as a connection whose device reads nothing, which would otherwise hold the stop for ever. The replica then waits up to `close_timeout` for its sessions to forget their presence, before Redis closes. A presence that a stop cut short anyway lapses within `presence_ttl`, and the registry holds nothing of a session.
8. **Sizes and pings:** uvicorn's `ws_max_size` is the protocol's 1 MiB, so a larger message closes its connection with 1009, as aiohttp does on the robot's side. uvicorn's own WebSocket pings are off: the protocol pings with messages of its own, which a proxy cannot answer for the device.
9. **A deleted home's devices: the registry alone decides,** at the control channel as at the token endpoint. Until the worker's consumer revokes them, they get tokens and connect; once it has, its hint ends their sessions, and the checks do if the hint is lost. The channel does not ask the accounts whether the home still exists:
   - the token endpoint and the channel would then disagree: a device with a token that its channel refuses tries again and again, or, told `revoked` while the registry holds its key, offers set-up only to be `device_taken` elsewhere;
   - it would cover only connections opened within the lag, while devices stay connected, and every module's data of the home lags by the same delivery (ADR-0009), which the worker's retries bound;
   - it would make each connection read another module's tables, which can move to a database of their own (ADR-0001).
10. **Each hello opens a new session** (`resumed: false`) in this step. Resuming one, with its numbers and what it sent kept in Redis for `resume_window` so that another replica can take it, is the next step. Its first answer to an oversized message comes with it, as the robot gives its own.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Hints alone | Redis loses a hint during a subscriber's disconnection: a removed device would stay connected until its connection dropped. |
| Checks alone | Prompt only with a short interval, which costs a query and a pipeline for every connection that often; a removed device should offer set-up at once. |
| Hints on Redis Streams, as Socket.IO's adapter | Every replica would read every entry, and keep its place and the stream's trimming; the checks already bound what a lost hint costs, for nothing more. |
| A channel for each device | Each connection would subscribe and unsubscribe. It is how hints shard (`SSUBSCRIBE`) once one channel is too busy, behind the same port. |
| The domain events for hints | A consumer group hands each event to one of its consumers, in the worker, not to every replica, and only after the relay's poll. |
| Presence in PostgreSQL | A write for each connection and each check, for what may be lost (ADR-0008). |
| Presence as connected and disconnected events | They come out of order, or twice, as AWS IoT warns. A key naming the newest connection, compared before it is changed, needs no order. |
| `SET … IFEQ` and `DELEX` | redis-py 8.1 marks them experimental; the scripts work on any Redis, as Redis's own lock pattern does. |
| Asking the accounts at the handshake whether a device's home exists | See decision 9. |
| Sticky connections at the proxy | A replica that restarts loses its connections all the same, and resuming needs the session outside the process. |
| `reconnect` from the application's lifespan | uvicorn has closed every WebSocket by then. |

## Consequences

- Positive:
  - any replica serves any device, and a newer connection or a revocation ends a session wherever it is held: as soon as a hint arrives, and within `check_interval` if it is lost;
  - `connected` is the same from every replica, and lapses with a replica that stops without closing its connections;
  - the session does not know WebSockets: another kind of channel is an adapter.
- Negative / accepted trade-offs:
  - a lost hint delays a `bye` by up to `check_interval`, 20 s;
  - each replica runs a query and a pipeline every `check_interval`, for all its connections;
  - `connected` stays true for up to `presence_ttl`, 60 s, after a replica stops without closing its connections;
  - a device connects only while Redis answers, as it gets a token only then;
  - until resuming comes, a connection's loss ends its session, and a message over 1 MiB its connection without an error.
  - a stop takes up to `close_timeout` and `shutdown_timeout`, 8 s, within the 10 s that Docker Compose waits by default; a connection cut then gets no close of its own;
  - a device cannot have JSON nested deeper than `[devices.link] max_json_depth`, 64, read: the cloud link names no depth yet, and 64 is .NET's System.Text.Json default (`JsonReaderOptions.MaxDepth`, learn.microsoft.com, read 2026-05-27).
- Follow-up:
  - resuming sessions, decision 10;
  - `update_required`, and 410 for a major version retired, with `updates`;
  - a channel of hints for each device, once one is too busy.

## References

Retrieved 2026-11-11:

- ai-platform: the cloud link 3.0 ([The control channel](https://github.com/trungnguyen278/ai-platform/blob/main/protocols/cloud/README.md#the-control-channel), Staying connected, Resuming a session, The session, Errors, Security and privacy), and the app API 1.4's [Devices](https://github.com/trungnguyen278/ai-platform/blob/main/protocols/app/README.md#devices).
- uvicorn 0.54.0: `uvicorn/server.py` (`Server.shutdown`) and `uvicorn/protocols/websockets/websockets_sansio_impl.py` (`shutdown`, the denial response, `max_size`).
- Redis: [publish and subscribe](https://redis.io/docs/latest/develop/pubsub/) (delivery semantics, sharded channels), [SET](https://redis.io/docs/latest/commands/set/) (`GET`, `IFEQ`, and the lock pattern), [DELEX](https://redis.io/docs/latest/commands/delex/) (since 8.4.0).
- Socket.IO: [the Redis Streams adapter](https://socket.io/docs/v4/redis-streams-adapter/).
- AWS IoT: [lifecycle events](https://docs.aws.amazon.com/iot/latest/developerguide/life-cycle-events.html).
- Centrifugo: [engines](https://centrifugal.dev/docs/server/engines), `presence_ttl`.
- Kubernetes: [controllers](https://kubernetes.io/docs/concepts/architecture/controller/).
- uvicorn's [settings](https://uvicorn.dev/settings/) (`--timeout-graceful-shutdown`) and Docker Compose's [services](https://docs.docker.com/reference/compose-file/services/) (`stop_grace_period`, 10 s by default), retrieved 2026-11-12.
- IETF: [RFC 8259](https://www.rfc-editor.org/rfc/rfc8259), 9, and [RFC 7493](https://www.rfc-editor.org/rfc/rfc7493), 2.2, retrieved 2026-11-12; [RFC 6455](https://www.rfc-editor.org/rfc/rfc6455), 7.4.1; IANA's [WebSocket close codes](https://www.iana.org/assignments/websocket/websocket.xhtml) (1012, Service Restart).
