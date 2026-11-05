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

### Security and privacy

- Every request is confined to the device that made it and the account it is bound to (SEC-6).
- No secret in the code, the configuration, the logs or an error message. Logs and health data carry no audio, images or conversations (PRIV-5).
- A log record's fields (`extra`) never take a name of `LogRecord`'s own, such as `module` or `name`: logging raises when the record is made, at a level the tests might not reach. The tests make every record, whatever its level.
- What is kept about an owner can be found and deleted (PRIV-4).

### Concurrency

- Nothing blocks an event loop: file and network I/O are asynchronous, or run in a worker.
- Transactions lock rows in one order, the one a cascade takes (an account, then its sessions, then their tokens; an account, then a home, then what is in it), so that none deadlocks with a deletion. A row deleted only to tidy up counts: it is held from then on, so it comes after every other lock, or goes to the `worker`. A race the code handles, such as two first sign-ins of one identity, has a test that runs it.
- A failure in one robot's session never stops another's.

### Domain events (docs/decisions/0009-domain-events.md)

- A change that other modules must learn of writes its event through its module's `emit`, in the change's own transaction, under the lock of the thing it is about, so that their order holds. A test shows that a change that fails writes no event.
- An event's data holds identifiers: never personal data, a secret or content, which would outlive the deletion it reports, for the retention.
- A handler is idempotent, and works in any order: a test gives it the same event twice.
- What goes to Redis, or another store, as JSON is ASCII, as `json.dumps` writes it by default: a lone surrogate, which a `json` column takes as an escape, cannot be encoded as UTF-8, and would hold up every event behind it.
- A blocking read, such as Redis's `BLOCK`, ends before the client's own timeout, which would cut it short as if the server had failed: the configuration checks it.
- Work that several workers may do on the same rows at once, such as a sweep, first takes its turn, under a transaction-level advisory lock.
- A check that compares two stores, such as a stream's last place with its outbox's, runs under the lock of the change that could hide the difference, and before that change: a check after the outbox gave new places missed a restored outbox, and lost its events.

### Tests

- Each behaviour change has a test that would fail on the old code; check it is not vacuous (an exact float compare that never matches, a loop that never runs).
- No fixed sleeps to wait for readiness: wait for the condition, with a timeout.

### Repository rules (CONTRIBUTING.md, AGENTS.md)

- English everywhere; SPDX header on new source files; no personal or machine-specific values; no product name.
- Docs travel with the change: the README, ADRs (`Proposed` unless explicitly agreed), the dev log.
- `<type>(<scope>): <summary>`, one commit per pull request, `<type>/<short-description>` branch.

## 4. Confirm, then fix

For each suspicion, write the smallest script that shows it. Drop whatever does not reproduce. A search that finds nothing proves nothing until it has found a sample planted for it: a regular expression in a shell command can lose a backslash on the way. Fix what does in the pull request's single commit (`git commit --amend`, `git push --force-with-lease`), adding a test for it.

## 5. Report

When handing the change over, say in the conversation what the review found and how each item was fixed, or why it was left, and what was not checked. Keep it out of the commit message and the pull request description, which stay short (CONTRIBUTING.md).
