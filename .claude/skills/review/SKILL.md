---
name: review
description: Review a pull request or the current branch of this repository before handing it over; run it inline, without subagents, to keep the cost low. Use before telling the user a pull request is ready, and when asked to review a PR, a branch or a diff here.
---

# Reviewing a change

Goal: find the bugs before the user does, at a reasonable cost. Work in the main conversation: no subagents, no fan-out. Read the diff once, run the checks, walk the areas the diff touches, and confirm each suspicion by running code before reporting it. Add to this checklist each kind of bug the project meets, so that it is not met twice.

## 1. Scope it

```sh
git fetch -q origin
git diff --stat origin/main...HEAD     # which areas, how big
git diff origin/main...HEAD            # read it once, in full
```

Read whole files only where a hunk cannot be judged alone (an enclosing function, a caller). Note which areas below the diff touches; skip the others.

## 2. Run the checks

The linters and the tests the README lists, as CI runs them. A failing check is a finding; fix it before reading further.

## 3. Walk the areas the diff touches

### Data crossing a boundary (the robot, the app, a third-party service)

- The protocol changed first, in ai-platform, and the submodule's pin moved with the code that speaks it. A pin moves to a commit that may hold other protocols' versions too, such as `cloud/v3.4` holding the app API 1.7: the whole suite runs on the new pin before anything is built on it.
- Every message received is validated: its schema, its size, its numbers (finite), its strings (length); what fails is refused (SEC-14). Rates and sizes are limited (SEC-15).
- Schemas are checked through `shared/protocols.py`, never a bare validator: Python's `$` also matches before a last newline, which JSON Schema's does not.
- Text can hold a lone surrogate, which JSON's escapes can spell and no driver can store, or a U+0000, which PostgreSQL's `text` cannot hold (a `500` in the driver): JSON read any other way than `read_json` (and `json_body`, which uses it) must refuse both, in names as in strings, at every depth (ai-platform ADR-0014). A check of the value that a parser returns misses what the parser dropped: `json.loads` keeps the last of a repeated name, which is why `read_json` refuses repeated names. What else I-JSON forbids, the cloud holds: a test shows which.
- A frame that the protocol says cannot be answered, such as a binary frame, a numbered type without its number or a `ping` that fails, ends the session as the protocol says, before the hello as after it: one ignored may have taken a number, and shifts what follows to `out_of_sequence` or to a duplicate dropped unseen.
- A server that closes a WebSocket itself, as uvicorn does with 1009 past `ws_max_size`, still delivers the close to the application, with its code: what the application sends or closes after it raises, unless the adapter checks the client's state first. The code is how the session ended, and a test closes with each code that means something, from both sides: the cloud's close, and the device's own, which the protocol reads alike (1002, 1007, 1008 and 1009 since the cloud link 3.6).
- A replayed message has no effect beyond its first one, and credentials expire (SEC-12).
- A credential that may be presented again, such as a refresh token retried after a lost answer, still gives theft away: what it replaces stops working, and that one presented again ends the session. Deleting it instead would turn the thief's next step into a plain refusal.
- What a token says before it is verified, such as a registration's challenge, is checked for its form before anything uses it: a lone surrogate in it fails as a `500` where it is hashed or stored. The whole token is verified after.
- What the cloud keys on, such as a device's key, is read one way only: base64url strictly, without padding or bits left over, and a curve's coordinates below its prime. PyJWT's decoding and `cryptography` both accept more, and one key would have two thumbprints.
- A request that uses something once, such as a challenge, keeps its answer for a retry whatever it is, a refusal included, in the transaction of the use. One that used nothing keeps nothing.
- A change's condition is read where each version of the protocol puts it: the app API 1.7's `etag` in the body, compared as written, and the `If-Match` of apps of 1.6, a list or `*`. When both come, each is checked, `If-Match` first (RFC 9110, 13.2.1). A test sends each, and both, at every endpoint that takes them.
- A limit that the protocol fixes, such as the 64 KiB of a body, is a named constant beside its citation, never a setting, and a test sends the limit and one byte more, with and without a length.
- A first answer kept for a retry was written by the API of its day: a field added since, such as a device's `etag`, is missing from it, and what gives it again must not count on it.
- PyJWT checks a token's times with `int()`, which takes the text of a number, and `true`. A claim that the code computes with is checked as a JSON number first.
- What a device must hear before its connection closes, such as `reconnect`, is sent before uvicorn's own shutdown, which closes every WebSocket itself before the application's lifespan ends.
- JSON from outside is read with `read_json`, never `json.loads`, which raises `RecursionError` at a depth that the machine's C stack sets (11,000 on Windows, past 20,000 on Linux: a test of a refusal there must not count on it), not a `ValueError`, and reads `1e400` as an infinity that no `parse_constant` sees. The depth is the protocols' (`MAX_DEPTH`, 64, ADR-0014), counted on the text before it is parsed, so that the answer is the same on every machine.
- A parsed value is used only once its type is known, or after the schema: `kind in a_set` raises `TypeError` for a list or an object, and `.get` for anything that is no object. A test sends each JSON type where the code reads one.
- A limit that a library checks, such as websockets' `max_size`, counts as the protocol does only where a test shows it: with permessage-deflate, websockets 17.3 compares a compressed frame's length with what the message may still hold, and refused a message of exactly 1 MiB. A test sends the protocol's largest message, and one byte more, as a client may send them: in one frame, in fragments, and with each extension that it offers.
- A check that collects every error before it chooses one takes the time of the whole value: jsonschema's `best_match` over `iter_errors` built 75,000 errors for a 1 MiB array. `Schema.problem` names the first rule broken, as `is_valid` stops at it.
- Each bug class met in reading or validating gets a vector in ai-platform's `vectors/`, which every side runs (`tests/shared/test_vectors.py`), each within its file's `seconds`.
- A schema keyword's cost is the library's: jsonschema's `uniqueItems` compares each item with every other when they cannot be sorted, recursively, so `shared/protocols.py` checks it only where `maxItems` and `items` allow the array, and the protocols bound every such array.
- A regular expression that can fail after a long match is tried again from each later start: a JSON string that requires its closing quote took 6.4 s over 20,000 escaped quotes. A pattern over what a device or an app sends cannot fail after it matched far (`read_json`'s strings run to the end of the text without their quote), and a test times the worst text.
- Whatever a session holds open is bounded, the server's stop included: uvicorn waits for every connection without `[api] shutdown_timeout`.
- A close is bounded by the side that closes: asyncio's `close` waits for the peer to read what is buffered, for ever if it reads nothing, and uvicorn's sans-I/O protocol has no bound of its own. Every way that uvicorn closes goes through the transport of `entry/websocket.py`, which aborts after `[api] ws_close_timeout`; a test of a peer that reads nothing runs against the real server, whose layout an upgrade may change. A bound that a document states adds up every wait before it, such as the session's own `close_timeout` before the transport's. Every end of a connection is bounded, not only the session's: a refused hello, a bye or another message before the hello, a hello that never comes, and a reconnect as the session starts each sent and closed without a deadline, and a device that read nothing held its connection 42 s. A test ends each way against a channel that takes nothing.
- A limit comes before the step it spares, and after the token's check: whatever is counted before a valid token is anyone's to spend, and a host without one then refuses every device (the replica's count of every handshake did, at 619 attempts a second). The token's signature is checked first, without I/O; the device's own count next, then the replica's, then the registry. A test floods with no token, a forged one and a device past its own rate, then connects another device. A limit counts what the client cannot change, the device that a token names, not the token: a test tries again with a new one.
- What a device sends is limited in its length as well as its messages, for the device, not for each connection: reading 1 MiB costs from 11 to 190 ms by its shape, small numbers the worst, and a count that each connection starts whole lets a device read a burst for each connection it opens. Its first message, the hello, counts too.
- What a WebSocket carries below the application is bounded too: uvicorn 0.54 handles each Ping, Pong and continuation frame as it parses it, which no rate of the session sees, writes each Pong whether its transport takes more or not, and parses the rest of a read after a message that waits for the application. `entry/websocket.py` bounds them as the cloud link's Frames say (since 3.4) and parses no further than the application takes; a test floods each kind against the real server, and one that reads nothing.
- A bound below the messages refuses no message that the protocol lets through: a rate for every frame closed two messages of 1 MiB in fragments of 1 KiB, back to back, which Sizes allows. A test sends the largest messages, back to back, in the smallest fragments the protocol allows, against the real server; a bound that the protocol fixes is a named constant that cites it, not a setting.
- A dependency that fails inside a handshake, such as Redis at a device's count, answers as the protocol says, with Problem Details (503), not uvicorn's plain-text 500 and its traceback for each attempt.
- Nothing anyone can send writes a line for each attempt: a refusal is not logged by the routes, and uvicorn 0.54 logs each handshake at INFO whatever `access_log` says, has websockets log another, logs an error for each refusal, and a warning for each request that it cannot parse or upgrade. A test counts what a refusal and an unserved request write, with `access_log` off and on. A peer's error is no error of the server's either: uvicorn 0.54 logs text that is no UTF-8 as an error with its traceback, and a frame that breaks WebSocket's rules before the handshake is answered trips its assertion, which asyncio logs as a fatal error; a test sends each, before and after the handshake, and finds no error logged. A filter that drops a peer's line keeps the same line where it is the server's own bug, such as httptools' `HttpParserCallbackError`, an exception in one of uvicorn's callbacks, and tells it by the exception that it wraps: a peer reaches the same class with a URL whose port httptools' URL parser refuses inside uvicorn's callback.
- A test of uvicorn's log lines can see them: its default logging keeps `uvicorn.error` from the root logger, where `caplog` listens, so such a test passes whatever is logged. Build its `Config` with `log_config=None`, as `entry/api.py` does, or attach `caplog.handler` to that logger; and a mutant that logs proves it.
- What websockets answers by itself, a Pong or a close's answer, comes in one `data_to_send` for every frame of a read: what holds back part of it holds back all, and a later call finds it empty. A test sends two Pings, and a Ping with a close, in one read.

### Security and privacy

- Every request is confined to the device that made it and the account it is bound to (SEC-6).
- No secret in the code, the configuration, the logs or an error message. Logs and health data carry no audio, images or conversations (PRIV-5).
- A log record's fields (`extra`) never take a name of `LogRecord`'s own, such as `module` or `name`: logging raises when the record is made, at a level the tests might not reach. The tests make every record, whatever its level.
- What is kept about an owner can be found and deleted (PRIV-4). Each of the accounts' events, a home deleted, a person removed, an account deleted, has a consumer in every module that keeps something of it: a person who leaves a home takes along what they asked for there.

### Concurrency

- Nothing blocks an event loop: file and network I/O are asynchronous, or run in a worker.
- Transactions lock rows in one order, the one a cascade takes (an account, then its sessions, then their tokens; an account, then a home, then what is in it), so that none deadlocks with a deletion. A row deleted only to tidy up counts: it is held from then on, so it comes after every other lock, or goes to the `worker`. A race the code handles, such as two first sign-ins of one identity, has a test that runs it.
- A failure in one robot's session never stops another's.
- What a session keeps for its own use, such as what it has logged once, has a bound that the device cannot raise: fixed keys, never one for each value the device names.
- A token bucket whose cost can exceed its burst, such as a message's length, waits for a full bucket and lets the rest be owed: one that waits for the whole cost never takes it.
- A check that refuses new work once a replica goes away is made again after each wait before the work starts, such as the handshake's read of the registry: `going_away` may come during it.
- What every replica shares about one owner, such as the presence that names a device's newest connection, changes only while it still names the one changing it, by a script that compares and writes at once: a connection that closes late must not take its successor's away.
- A replica holds what a hint must reach, such as a session, before its last check of the source of truth, so that a change committed after the check is hinted after it. A hint names what to check and carries no verdict, and a check every interval finds what a lost one would have said: Redis's publish and subscribe delivers once at most.
- A session that ends itself after a change it makes, such as a leave that removes its device, is not ended first by the check that the change brings: its own hint reaches its own replica, and the check every interval may run between the commit and the end. The check leaves such a session to it until it ends or fails, and a test runs the check inside the change.
- A statement that takes a set that grows with use, such as every device a replica holds, takes it as one array (`= ANY`): `IN` spends a parameter on each, and asyncpg takes 32,767 at most.
- Rows that two transactions may delete at once, such as a home's and its owner's account's, are held in the order of their primary keys (`SELECT … ORDER BY … FOR UPDATE`) before they are deleted. Deleted through two indexes, they are taken in two orders, and deadlock once the tables are large enough that PostgreSQL reads by index. A test holds one of the rows, and looks at what the deletion holds while it waits.
- What is stored after a check of another module, such as a challenge after the person's role, is checked again once stored: a deletion between the two would leave it behind.
- A module that forgets what another's deletion took locks none of that module's rows: it first deletes the rows that every change in flight holds, such as its challenges. Read Committed then makes it wait for those changes, and its next statement sees what they added. Tests run both orders: a change in flight, and one that comes after.

### Domain events (docs/decisions/0009-domain-events.md)

- A change that other modules must learn of writes its event through its module's `emit`, in the change's own transaction, under the lock of the thing it is about, so that their order holds. A test shows that a change that fails writes no event.
- An event's data holds identifiers: never personal data, a secret or content, which would outlive the deletion it reports, for the retention.
- A handler is idempotent, and works in any order: a test gives it the same event twice.
- A handler logs what it changed, and nothing for an event that changed nothing: every worker reads every module's events.
- What goes to Redis, or another store, as JSON is ASCII, as `json.dumps` writes it by default: a lone surrogate, which a `json` column takes as an escape, cannot be encoded as UTF-8, and would hold up every event behind it.
- A blocking read, such as Redis's `BLOCK`, ends before the client's own timeout, which would cut it short as if the server had failed: the configuration checks it. A read that waits for as long as it takes, such as hints on a channel, says so (`get_message(timeout=None)`), and a test lets it wait longer than the client's timeout.
- Work that several workers may do on the same rows at once, such as a sweep, first takes its turn, under a transaction-level advisory lock.
- A check that compares two stores, such as a stream's last place with its outbox's, runs under the lock of the change that could hide the difference, and before that change: a check after the outbox gave new places missed a restored outbox, and lost its events.

### Tests

- Each behaviour change has a test that would fail on the old code; check it is not vacuous (an exact float compare that never matches, a loop that never runs).
- A test that holds a step open, such as a registry's read that waits for its release, bounds its own wait: a regression that reaches the step would hang instead of failing.
- No fixed sleeps to wait for readiness: wait for the condition, with a timeout. A race test waits until the other side is blocked on its lock, as `pg_stat_activity` shows, before it lets the first go.
- A test that compares a module's tables with what it declares looks at that module's schema only: the other modules' tables show as removed.
- A test's values keep to the protocol's patterns, which a pin can tighten: revoking `not a token`, with spaces, became a form that the app API 1.8 refuses, and tested the refusal instead of the answer it meant. The whole suite runs on a new pin, and the forms' vectors go to the real endpoints as the app writes them (WHATWG's form encoding, which percent-encodes `~` where Python's `urlencode` does not). A pin can loosen a rule too: a test that expects a refusal, or a vector's valid branch that expects one, meets the answer instead, as the app API 1.9's hints did.

### Repository rules (CONTRIBUTING.md, AGENTS.md)

- English everywhere; SPDX header on new source files; no personal or machine-specific values; no product name.
- Docs travel with the change: the README, ADRs (`Proposed` unless explicitly agreed), the dev log.
- A value that is one deployment's, such as an address, has none in `base.toml`: a value for this machine there would pass for production's. The development environment's file has it, and a deployment's `local.toml`.
- `<type>(<scope>): <summary>`, one commit per pull request, `<type>/<short-description>` branch.

## 4. Confirm, then fix

For each suspicion, write the smallest script that shows it. Drop whatever does not reproduce. A search that finds nothing proves nothing until it has found a sample planted for it: a regular expression in a shell command can lose a backslash on the way. Fix what does in the pull request's single commit (`git commit --amend`, `git push --force-with-lease`), adding a test for it.

## 5. Report

When handing the change over, say in the conversation what the review found and how each item was fixed, or why it was left, and what was not checked. Keep it out of the commit message and the pull request description, which stay short (CONTRIBUTING.md).
