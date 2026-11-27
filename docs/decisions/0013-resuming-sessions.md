# 0013 — Resuming a session: one record a device in Redis, written only by the connection that holds it

- **Status:** Accepted, 2026-11-27: the owner left the choice to the coordinator.
- **Date:** 2026-11-24, revised 2026-11-27 after the review of ai-cloud#37

## Context

- **What is decided already:**
  - the cloud link 3.7 (ai-platform aaf97a5, `cloud/v3.7`), Resuming a session: a device whose connection was lost names its session and the last number it received in its next hello. The cloud answers with the same session, `resumed: true` and the last number it handled, then sends again what the device did not receive, if it still holds the session, "on an older connection or for `resume_window` seconds after the loss", as the device's own under the same registration (SEC-6), and kept all the device has not received. Otherwise the device gets a new session;
  - a newer connection that resumes a session that an older one holds takes it over: the cloud closes the older with 4000 and no `bye`, handles nothing more from it, and only then answers. A `bye`, or a close with 1002, 1007, 1008 or 1009, ends a session for good. The session's terms, its `protocol`, `device`, `conversations` and `media`, stay as its first hello set them: a resume that changes them gets a new session;
  - [ADR-0011](0011-control-channels.md): a session lives in the replica that holds its connection, the presence and the hints in Redis, and its decision 10 left resuming to this step. [The architecture](../architecture.md): replicas keep no state, and Redis holds only what can be lost.
- **When resumes come.** The robot closes after one ping interval without a `pong`, the cloud only after two intervals of silence: most resumes come while the cloud still holds the older connection, and any replica may get them.
- **Prior art** (read 2026-11-24):
  - **MQTT 5** keeps a session after its connection closes for its Session Expiry Interval (§3.1.2.11.2), resumes it for a CONNECT with Clean Start 0 (§3.1.2.4), and sends the older connection of the same client a DISCONNECT "Session taken over", 0x8E (§3.1.4).
  - **EMQX** takes a session over in two phases (`emqx_cm`, `takeover_session_begin` and `takeover_session_end`): the older channel stops, hands its session and what it had yet to deliver, and the newer goes on from it. It waits `T_TAKEOVER`, 5 s, for the older channel.
  - **AWS IoT Core** keeps a persistent session for an hour by default, configurable up to seven days, with the messages queued for its client.
  - **Socket.IO's connection state recovery** keeps a socket's id, rooms and missed packets for `maxDisconnectionDuration`, two minutes in its example; a recovery that fails gives a new socket, with `socket.recovered` false.

## Decision

1. **One record a device,** a Redis hash, `devices:session:<device>`: its session, the registration it began under, the SHA-256 of its terms (their canonical JSON, so that a resume compares them all while a device's own fields in its hello cost the record 64 characters, not the megabyte a hello may hold) and its version, the last number handled from the device and the last one sent, what it kept of what it sent, the connection that holds it (`owner`) and the newer one that claimed it (`taker`). A new session of the device replaces it. Lost with Redis, it costs a new session only.
2. **Only the connection that holds it writes it,** through Lua scripts that compare the owner and write at once (`START`, `CLAIM`, `TAKE`, `HOLD`, `HAND`, `END`): a connection that ended, or lost the session to a newer one, writes nothing over it. The holder writes its numbers and what it kept once, as it gives the session up; until then they are the session's own, in its replica.
3. **How long:** a record held lasts `presence_ttl`, and each check renews it with the presence. Given up, it lasts `resume_window`, counted from when the cloud saw the loss.
4. **A resume takes only the device's own session** (its record is under the device's key), under the registration of its connection, on the same terms, of a version the cloud still speaks: the first of these that fails says why. Its `received` must then be within what the cloud kept: from the number before the first message kept to the last one sent. Otherwise the device gets a new session, and the log says why, never with a value of the device's.
5. **A takeover hands the session over.** The newer connection claims it and hints the device. The older connection's replica checks it, finds it claimed, and ends the session `taken_over`. The older then finishes the message it is handling, within its end's `close_timeout` (decision 11), and handles nothing more. It hands the record to the newer, closes with 4000 and no `bye`, and drops what it had yet to send, which the newer sends again. The newer waits for the hint of the hand-over up to `takeover_timeout`. Its last try withdraws its claim, so a hand-over that comes later parks the session, and both settle on a new one: the older ends with `bye` `replaced`, as for any newer session. A message whose handling had to be cancelled leaves the session unresumable (it may have done half), so it ends for good.
   - **A newer claim supersedes an older one,** which takes nothing and starts nothing in its place: its connection closes with 4000, for the session goes on on the newer one.
   - **A connection checks its record once it holds the session,** taken or started: a claim that came just before found no session held to hint, and is found then.
6. **`received` counts the messages handled,** not those read: a pause stops the handling, and what was read but not handled the device sends again. A message dropped past the backlog counts as handled, since its error answered it.
7. **What is kept:** each text sent, up to `replay`, until the device's ping says it received it.
8. **The ends:** a lost or closed connection (but for 1002, 1007, 1008 and 1009), silence, a stall, `reconnect` and a takeover pause the session, for it to be handed over or to wait for `resume_window`. Every other end deletes the record, and so does the device's removal, in the app, by its own leave or with its home, whoever holds the record: a removed device resumes nothing, and the cloud keeps nothing that was sent to it (PRIV-4). A leave that completes while its session pauses ends it for good.
9. **A resuming hello's other fields** apply: its time zone, languages and allowed classes. The cloud holds no conversation, so one that it names gets `conversation.end` `lost` after what is sent again. Its `events` are not read.
10. **The settings,** required in `[devices.link]`: `resume_window`, 120 s, the link's starting point and Socket.IO's example; `replay`, 1,000, the link's starting point; and `takeover_timeout`, 2 s, which the configuration keeps shorter than `hello_timeout`, so that the device does not give up on the cloud's hello first.
11. **One `close_timeout` for the whole end,** from when it begins: the message in hand, giving the record up in Redis, the last messages and the close, and leaving the presence, which uvicorn waits for before it cuts the connection, share it, so that [ADR-0012](0012-what-one-device-can-exhaust.md)'s bound holds, a device that reads nothing cut at most 9 s after its session ends, whatever the message in hand or Redis does: a call that Redis has not answered by then is given up. A record or a presence that Redis did not answer for lapses within `presence_ttl`, and no resume takes the record, for a resume takes only a session handed over or parked. The device still tells a hand-over from `bye` `replaced`: the newer connection's last try, within `takeover_timeout`, comes before the older's end is over, and withdraws its claim, so that a hand-over after it parks the session and the older ends with `bye` `replaced`. Only a Redis that answers a hand-over later than both blurs them, as an error would.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Write each message's numbers and text to Redis as it goes | A round trip for every message and every pong, while only the holder reads them; the hand-over is the one moment another connection needs them. |
| The session in PostgreSQL | Durable for what costs only a new session when lost; the registry is PostgreSQL's, and what may be lost Redis's (the architecture). |
| The newer connection takes the record as last written, and ends the older at once | The older may still be handling a message, whose answer and number the newer would never know: the link has the cloud handle nothing more, and only then answer. |
| No fence on the writes | A late write of the older connection, or of a replica cut off, would overwrite the newer's session. |
| A held record that lapses into a parked one | Redis expires a key whole, and its keyspace notifications are delivered once at most; the lease would need a sweeper of its own. |
| Keep a takeover's claim after its wait | A hand-over that came just after the wait would give the session to a connection that already started a new one. |

## Consequences

- Positive:
  - a robot keeps its session through a Wi-Fi drop, a replica's restart (`reconnect`) or a takeover, on whichever replica it reaches, and gets what it missed;
  - Redis keeps what a session needs, and only while it can be resumed.
- Negative / accepted trade-offs:
  - a held session whose record Redis lost goes on, but its device gets a new session when it next connects;
  - a replica that crashes, or one cut off from Redis, hands nothing over: a resume of its sessions waits `takeover_timeout`, then gets a new session, and the records lapse within `presence_ttl`;
  - a hand-over whose message in hand had to be stopped makes the newer connection wait the whole `takeover_timeout`, for the older ends the record without a hint;
  - `close_timeout`, 5 s, is longer than `takeover_timeout`, 2 s: a message handled for longer than that gives the device a new session;
  - a message handled to the end of `close_timeout` leaves its session no time to end its record, which lapses within `presence_ttl`;
  - a record held keeps up to `replay` texts while its device is away. Those texts are the cloud's own messages, errors and `conversation.end` today, and its device says what it received every `ping_interval`.
- Follow-up:
  - what a session sends again grows with the modules that send calls and events.

## References

Retrieved 2026-11-24:

- ai-platform: the cloud link 3.7, [Resuming a session](https://github.com/trungnguyen278/ai-platform/blob/main/protocols/cloud/README.md#resuming-a-session), its examples `resume.json` and `takeover.json`, and the four vectors of resuming hellos.
- [MQTT 5.0](https://docs.oasis-open.org/mqtt/mqtt/v5.0/os/mqtt-v5.0-os.html), §3.1.2.4, §3.1.2.11.2 and §3.1.4.
- EMQX, [`emqx_cm.erl`](https://github.com/emqx/emqx/blob/master/apps/emqx/src/emqx_cm.erl): the takeover's two phases and `T_TAKEOVER`.
- AWS IoT Core, [MQTT persistent sessions](https://docs.aws.amazon.com/iot/latest/developerguide/mqtt.html#mqtt-persistent-sessions).
- Socket.IO, [Connection state recovery](https://socket.io/docs/v4/connection-state-recovery).
