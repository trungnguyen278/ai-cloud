# 0010 — Registering devices: challenges in PostgreSQL, used in the registration's transaction

- **Status:** Accepted
- **Date:** 2026-11-05

## Context

- **What is decided already:**
  - ai-platform's [ADR-0012](https://github.com/trungnguyen278/ai-platform/blob/main/docs/decisions/0012-registering-devices.md) and its app API 1.4: the owner or an admin gets a challenge for a home, the device signs its registration with it, and the app relays it with an `Idempotency-Key`. A challenge works once, *whether or not the cloud then registers the device*, and a retry gets the first answer. Deleting a home revokes its devices.
  - [ADR-0008](0008-data-and-tokens.md), decision 3: Redis holds sign-in's nonces, consumed with `GETDEL`, since "they live minutes and may be lost".
  - [ADR-0009](0009-domain-events.md): `accounts.home.deleted.v1` reaches the `devices` module through the worker, at least once, in any order, some time after the home went.
- **What must hold:**
  - a registration's use of its challenge, and the answer that a retry gets, stand or fall together. A crash between the two must neither burn a challenge without an answer, nor keep an answer without the challenge used;
  - **no registration or challenge racing the home's deletion outlives it,** nor one racing a person's leaving the home, or the account's deletion. Either may have checked the home before the deletion, and reach the devices' tables after the event's handler ran. The home is the accounts' row, which `devices` must not lock: a module's tables can move to a database of their own (ADR-0001);
  - a home's limit holds under registrations that race, and one device lands in one home when two homes take it at once;
  - nothing of a deleted home, or of a deleted account, stays (PRIV-4).
- **How others keep such a challenge** (retrieved 2026-11-05): a WebAuthn relying party keeps the challenge it issued, and checks that the registration answers it: "Verify that the value of C.challenge equals the base64url encoding of pkOptions.challenge" ([Web Authentication, Level 3](https://www.w3.org/TR/webauthn-3/), §7.1, step 8). The IETF's draft leaves the scope of an Idempotency-Key to the resource: "Uniqueness of the key MUST be defined by the resource owner" ([draft 08](https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/), §2.2).
- **PostgreSQL 18, Read Committed** ([§13.2.1](https://www.postgresql.org/docs/18/transaction-iso.html), retrieved 2026-11-05): a `DELETE` that finds a row another transaction deleted "will wait for the first updating transaction to commit or roll back", then "ignore the row" if it was deleted; and each command starts "with a new snapshot that includes all transactions committed up to that instant".

## Decision

1. **Challenges are kept in PostgreSQL,** in `devices.challenges`, as their SHA-256, with their home, their account and their expiry.
   - **Issuing one checks the person twice:** their role in the home, then, once the challenge is stored, again. A deletion of the home, of the person's place in it, or of the account, that came after the first check either finds the challenge and forgets it, or comes before the second check, which forgets it and answers as if the home did not exist.
   - **A registration uses its challenge by deleting it,** in its own transaction, and keeps its answer there too: a refusal that comes after, such as `device_taken`, is kept as the first answer as a success is. A registration whose challenge it cannot use keeps nothing, and changes nothing.
2. **A registration and a removal hold their home,** under a transaction-level advisory lock named after it, so that the home's limit holds, and a removal never deletes a device that a registration's answer is about to show. A registration then uses its challenge, and adds its device and its answer; a removal deletes the device's first answers, then the device.
3. **A home's deletion, a person's leaving a home, and an account's deletion hold nothing: each deletes their challenges first,** then their first answers, then a home's devices. A registration that used one of the challenges holds its row until it ends: the deletion waits for it, and its next statement sees what the registration added. One that comes later finds its challenge gone. So nothing that a registration racing a deletion keeps outlives it.
   - **Every deletion takes its rows in one order:** challenges, then first answers, then devices, and the rows of each in the order of their primary key, held with `SELECT … ORDER BY … FOR UPDATE` before they are deleted. A removal deletes a device's first answers before the device. So two at once, as an owner's account and the home it takes along, never wait for each other in a circle. Deleting through each table's indexes took the rows they share in the orders of two indexes: with 20,000 other rows, so that PostgreSQL read by index, the two deadlocked in 8 rounds of 10.
4. **A device's keys have a table of their own,** `devices.keys`, by thumbprint, which its assertions name as their `kid`: the cloud link lets a device hold two while it changes them. A key is one device's, and a device is in one home, by their primary keys; a race between two homes ends in a unique violation, answered `device_taken`.
5. **First answers are each module's,** in its schema, as the accounts' are. An account's key used for a home and for a device is two keys.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Challenges in Redis, consumed with `GETDEL`, as sign-in's nonces | Their use and the answer would be in two stores: a crash between them burns a challenge without its answer, or the other way round. And nothing in Redis can stop a registration that checked the home before its deletion, and adds its device after the event's handler ran. |
| Locking the accounts' home row from the registration | The transaction would span two modules' tables, which ADR-0001 keeps apart. |
| A table of deleted homes, which registrations check | It grows for good, or needs a retention longer than any registration's, and adds a check to every registration that the challenges already give. |
| The deletion's handler holding the home's advisory lock | Its challenges, deleted first, already order it with every registration: the lock would add a wait and nothing else. |
| Holding the home while the person's role is checked, to issue a challenge | The check is the accounts', on a connection of its own: a request holding one connection while it waits for another can exhaust the pool under load. The second check needs neither. |
| Leaving a challenge issued across a deletion to the sweep of expired ones | Nothing would delete it until the sweep exists, and the second check costs one query. The sweep still takes one whose request stopped between the two. |
| Keeping the first answer of a success only, as the accounts do | The challenge is used by a refusal too: a retry after a lost refusal would get `registration_invalid` in place of what happened. |
| A device's key as a column of `devices` | The cloud link's `kid` exists so that the cloud "can know two of a device's keys while it changes them": a second key would need a new table, and its data moved. |
| One table of first answers for the whole cloud | It would be no module's, or would tie one module's transactions to another's schema. |

## Consequences

- Positive:
  - a challenge, its use and the answer stand or fall together, and a retry after any lost answer gets the same;
  - a home's deletion leaves nothing that a registration racing it added, without either module locking the other's rows;
  - a device can hold a second key without a migration of its first.
- Negative / accepted trade-offs:
  - a challenge that expires unused stays until the worker sweeps it, as old first answers do, and so does one whose request stopped between storing it and checking again, which no registration can use;
  - a person demoted or removed just after the check of their role can still finish a registration that had started: the device then belongs to the home, which its owner and admins see and can remove;
  - an app that used one Idempotency-Key for a home and then for a device would not get `422` from the second: the app makes a new key for each request.
- Follow-up:
  - the worker's schedules sweep expired challenges, with idle sessions and old first answers;
  - the token endpoint finds a device's key by its thumbprint; the control channel ends a removed device's session with `bye` `revoked`.

## References

Retrieved 2026-11-05:

- ai-platform: [ADR-0012](https://github.com/trungnguyen278/ai-platform/blob/main/docs/decisions/0012-registering-devices.md), and the app API 1.4's [Devices](https://github.com/trungnguyen278/ai-platform/blob/main/protocols/app/README.md#devices).
- PostgreSQL 18: [transaction isolation](https://www.postgresql.org/docs/18/transaction-iso.html) (Read Committed), and [advisory locks](https://www.postgresql.org/docs/18/explicit-locking.html#ADVISORY-LOCKS).
- IETF: [the Idempotency-Key HTTP header field](https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/), draft 08, §2.2 and §2.7.
- W3C: [Web Authentication, Level 3](https://www.w3.org/TR/webauthn-3/), §7.1 (registering a new credential).
