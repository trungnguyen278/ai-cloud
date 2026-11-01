# 0008 — Data and tokens: SQLAlchemy Core on asyncpg, Alembic, Redis, and PyJWT

- **Status:** Accepted
- **Date:** 2026-11-01

## Context

- **[ADR-0001](0001-architecture.md) sets where data lives:**
  - PostgreSQL is the source of truth, with a schema and migrations for each module;
  - Redis holds only what can be lost: caches, rate limits, presence, the queue.
- **[ADR-0002](0002-stack.md) shares across the process roles** the access to storage and the JOSE code, but chooses neither.
- **The first module to store anything is `accounts`,** for the app API 1.0 (ai-platform's `protocols/app/`, ADR-0008 there). It needs:
  - accounts, identities and sessions;
  - refresh tokens that rotate, and that a second use revokes;
  - nonces used once within minutes, and rate limits on asking for them;
  - Google's and Apple's ID tokens verified against their published keys (RS256);
  - the cloud's own access tokens, signed with keys that rotate (the architecture's Security).
- **Next, `devices`** verifies devices' assertions (ES256, the cloud link 3.0).
- **The candidates,** each released within the last four months (PyPI, as of 2026-11-01):

  | Need | Library | Version | Licence |
  |---|---|---|---|
  | SQL, async | SQLAlchemy | 2.1.6 (2026-10-21) | MIT |
  | PostgreSQL driver | asyncpg | 0.32.0 (2026-10-06) | Apache-2.0 |
  | | psycopg | 3.3.6 (2026-09-18) | LGPL-3.0 |
  | Migrations | Alembic | 1.20.1 (2026-10-22) | MIT |
  | Redis | redis-py | 8.1.0 (2026-07-30) | MIT |
  | JOSE | PyJWT | 2.15.1 (2026-09-28) | MIT |
  | | joserfc | 1.7.7 (2026-10-19) | BSD-3-Clause |
  | Containers for tests | testcontainers | 4.15.0 (2026-07-24) | Apache-2.0 |

- **PostgreSQL's current major** is 18 (18.6, 2026-08-13).

## Decision

1. **SQLAlchemy 2.1's Core, with its asyncio extension, on asyncpg.**
   - Each module declares its tables in its `db/`, in its own schema.
   - Queries are SQL expressions, written where they are used. There are no ORM sessions, so no lazy loading behind an `await` and no query hidden behind an attribute.
   - The same Core runs synchronously for Alembic.
2. **Alembic, one environment with a branch for each module.**
   - A module's migrations live with its tables, and its version table in its schema. A module can then move to a database of its own (ADR-0001).
   - **Migrations are a deployment step,** `ai-cloud migrate`, run before the new processes start, never by a process as it starts.
3. **Redis through redis-py's asyncio client,** for what may be lost:
   - nonces, kept with their expiry and consumed atomically (`GETDEL`), so that one works once;
   - rate limits.

   Refresh tokens are not among them: they must survive a restart, so they are in PostgreSQL.
4. **PyJWT, with `cryptography`,** for every signed token:
   - **ID tokens:** the providers' keys are fetched with httpx2, kept as long as the providers' caching headers allow, and fetched again for an unknown `kid`.
   - **Devices' assertions** are verified with ES256.
   - **The cloud's access tokens** are signed with ES256 and carry a `kid`. The keys rotate; the newest signs, and the ones before it verify until their tokens have expired.
   - **Refresh tokens** are 32 random bytes, kept only as their SHA-256, never in clear.
5. **Tests run against the real thing:** PostgreSQL 18 and Redis in containers, through testcontainers, on a developer's machine with Docker and in CI. No other database stands in for them.

## Alternatives considered

| Alternative | Why not |
|---|---|
| psycopg 3 as the driver | The PostgreSQL driver most projects start with, sync and async alike. asyncpg is faster for async, and its licence is permissive. SQLAlchemy speaks both, so a change would be one URL. |
| SQLAlchemy's ORM | Its sessions track objects and load relations on access. Under asyncio, a relation not loaded ahead fails at the `await`, and a query hides behind an attribute. The modules' data is small and their queries few: Core keeps each one in sight. |
| asyncpg alone, with SQL as strings | No composition of queries, no table definitions to migrate from, and migrations by a tool of their own. |
| SQLModel | A layer over SQLAlchemy and Pydantic, for models that are both tables and API shapes. The cloud's API shapes come from ai-platform's schemas, not from its tables. |
| joserfc | Complete JOSE, encryption included. The cloud only signs and verifies JWTs, which PyJWT does, and PyJWT is the most used. |
| Nonces in PostgreSQL | They live minutes and may be lost. ADR-0001 puts such data in Redis. |
| SQLite, or mocks, in tests | They differ from PostgreSQL in its schemas, types and locking. Tests would pass on a database that production does not run. |

## Consequences

- Positive:
  - every query is visible where it runs, and each module's data stays its own, down to its migrations;
  - every signed token passes through one well-known library, not code of our own;
  - tests exercise the database that production runs.
- Negative / accepted trade-offs:
  - the tests need Docker: Docker Desktop on Windows, which must be running, and the runners' Docker in CI;
  - six more dependencies, locked in `uv.lock`;
  - Core means writing the mapping from rows to the domain's types by hand.
- Follow-up:
  - the `accounts` module, with the app API 1.0;
  - Docker Compose for development, with PostgreSQL 18 and Redis, beside the processes of ADR-0004;
  - `devices` and its token endpoint, on the same JOSE code.

## References

Retrieved 2026-11-01:

- PyPI: [SQLAlchemy](https://pypi.org/project/SQLAlchemy/), [asyncpg](https://pypi.org/project/asyncpg/), [psycopg](https://pypi.org/project/psycopg/), [Alembic](https://pypi.org/project/alembic/), [redis](https://pypi.org/project/redis/), [PyJWT](https://pypi.org/project/PyJWT/), [joserfc](https://pypi.org/project/joserfc/), [testcontainers](https://pypi.org/project/testcontainers/).
- SQLAlchemy's [asyncio extension](https://docs.sqlalchemy.org/en/21/orm/extensions/asyncio.html), and why the ORM's lazy loading does not work under it.
- [PostgreSQL's versions](https://www.postgresql.org/support/versioning/).
- ai-platform's [app API](https://github.com/trungnguyen278/ai-platform/blob/main/protocols/app/README.md) and [ADR-0008](https://github.com/trungnguyen278/ai-platform/blob/main/docs/decisions/0008-sign-in.md).
