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

- The protocol changed first, in ai-platform, and the submodule's pin moved with the code that speaks it.
- Every message received is validated: its schema, its size, its numbers (finite), its strings (length); what fails is refused (SEC-14). Rates and sizes are limited (SEC-15).
- Schemas are checked through `shared/protocols.py`, never a bare validator: Python's `$` also matches before a last newline, which JSON Schema's does not.
- Text can hold a lone surrogate, which JSON's escapes can spell and no driver can store: a body read any other way than `json_body` must refuse it.
- A replayed message has no effect beyond its first one, and credentials expire (SEC-12).
- A credential that may be presented again, such as a refresh token retried after a lost answer, still gives theft away: what it replaces stops working, and that one presented again ends the session. Deleting it instead would turn the thief's next step into a plain refusal.
- What a token says before it is verified, such as a registration's challenge, is checked for its form before anything uses it: a lone surrogate in it fails as a `500` where it is hashed or stored. The whole token is verified after.
- What the cloud keys on, such as a device's key, is read one way only: base64url strictly, without padding or bits left over, and a curve's coordinates below its prime. PyJWT's decoding and `cryptography` both accept more, and one key would have two thumbprints.
- A request that uses something once, such as a challenge, keeps its answer for a retry whatever it is, a refusal included, in the transaction of the use. One that used nothing keeps nothing.
- A first answer kept for a retry was written by the API of its day: a field added since, such as a device's `etag`, is missing from it, and what gives it again must not count on it.
- PyJWT checks a token's times with `int()`, which takes the text of a number, and `true`. A claim that the code computes with is checked as a JSON number first.
- What a device must hear before its connection closes, such as `reconnect`, is sent before uvicorn's own shutdown, which closes every WebSocket itself before the application's lifespan ends.
- JSON from outside is read with `read_json`, never `json.loads`, which raises `RecursionError` at a depth that the machine's C stack sets (11,000 on Windows, past 20,000 on Linux: a test of a refusal there must not count on it), not a `ValueError`, and reads `1e400` as an infinity that no `parse_constant` sees. A depth that matters is a setting (`max_json_depth`), the same on every machine.
- A parsed value is used only once its type is known, or after the schema: `kind in a_set` raises `TypeError` for a list or an object, and `.get` for anything that is no object. A test sends each JSON type where the code reads one.
- A schema keyword's cost is the library's: jsonschema's `uniqueItems` compares each item with every other when they cannot be sorted, recursively, so `shared/protocols.py` checks it only where `maxItems` and `items` allow the array, and the protocols bound every such array.
- Whatever a session holds open is bounded, the server's stop included: uvicorn waits for every connection without `[api] shutdown_timeout`, and one whose device reads nothing never closes.

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
- A check that refuses new work once a replica goes away is made again after each wait before the work starts, such as the handshake's read of the registry: `going_away` may come during it.
- What every replica shares about one owner, such as the presence that names a device's newest connection, changes only while it still names the one changing it, by a script that compares and writes at once: a connection that closes late must not take its successor's away.
- A replica holds what a hint must reach, such as a session, before its last check of the source of truth, so that a change committed after the check is hinted after it. A hint names what to check and carries no verdict, and a check every interval finds what a lost one would have said: Redis's publish and subscribe delivers once at most.
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
- No fixed sleeps to wait for readiness: wait for the condition, with a timeout. A race test waits until the other side is blocked on its lock, as `pg_stat_activity` shows, before it lets the first go.
- A test that compares a module's tables with what it declares looks at that module's schema only: the other modules' tables show as removed.

### Repository rules (CONTRIBUTING.md, AGENTS.md)

- English everywhere; SPDX header on new source files; no personal or machine-specific values; no product name.
- Docs travel with the change: the README, ADRs (`Proposed` unless explicitly agreed), the dev log.
- A value that is one deployment's, such as an address, has none in `base.toml`: a value for this machine there would pass for production's. The development environment's file has it, and a deployment's `local.toml`.
- `<type>(<scope>): <summary>`, one commit per pull request, `<type>/<short-description>` branch.

## 4. Confirm, then fix

For each suspicion, write the smallest script that shows it. Drop whatever does not reproduce. A search that finds nothing proves nothing until it has found a sample planted for it: a regular expression in a shell command can lose a backslash on the way. Fix what does in the pull request's single commit (`git commit --amend`, `git push --force-with-lease`), adding a test for it.

## 5. Report

When handing the change over, say in the conversation what the review found and how each item was fixed, or why it was left, and what was not checked. Keep it out of the commit message and the pull request description, which stay short (CONTRIBUTING.md).
