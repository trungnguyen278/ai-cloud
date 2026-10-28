# 0001 — The architecture: a modular monolith, with ports and adapters

- **Status:** Proposed
- **Date:** 2026-10-28
- **Revised:** 2026-10-28, after a review for growth: the data's rules, skills as MCP servers, devices' links as ports, and how each part grows.

## Context

- The cloud serves every device of the project (ai-platform [ADR-0001](https://github.com/trungnguyen278/ai-platform/blob/main/docs/decisions/0001-repositories.md)). It must grow to more kinds of devices, more owners and more languages without being rebuilt.
- One person builds and runs it, on little money. Running local models is not possible, and paid APIs must be used sparingly.
- Its work differs in kind:
  - conversations, in real time, one per device in a LiveKit room;
  - requests over HTTPS, from devices and the app;
  - background work: events, schedules, notifications.
- Luni Cloud, the preview for the previous robot, was one FastAPI server and one AI service behind HTTP endpoints, with free-tier providers in fallback chains. Its agent ideas hold (a router, fillers, confirmations, barge-in, memory in tiers). It had no boundaries between its parts, and some of its providers were unofficial endpoints, which were blocked at times.
- How others are built:

| Project | Shape |
|---|---|
| Shopify | One codebase, cut into components with enforced boundaries: a modular monolith, chosen over microservices for their complexity. |
| Prime Video (stream monitoring) | Moved a service from distributed functions to one process, which cut its infrastructure cost by over 90%. |
| Home Assistant | One core, with integrations as plugins. Its voice pipeline (wake word, speech to text, conversation, text to speech) takes any provider for each stage. |
| xiaozhi-esp32-server | The open server for ESP32 voice devices, which Luni drew on. The parts of its speech pipeline are configurable modules. |

## Decision

1. **A modular monolith:** one Python package, cut into modules with enforced boundaries.
   - The modules are `accounts`, `devices`, `conversation`, `memory`, `skills`, `updates`, `operations` and `notifications`, with `shared` beneath them ([architecture](../architecture.md#modules)).
   - Each module owns its tables, in its own Postgres schema, and offers a public API (`api.py`). Nothing imports another module's internals; import-linter checks it in CI.
2. **Ports and adapters inside each module**, as on the robot. Every outside service is a port; configuration chooses its adapter; simulated adapters serve tests and running without accounts.
   - A device's links are ports too: the LiveKit room today, and links of other kinds for devices that hold no room.
   - Skills are MCP servers, in the process or apart, so that every tool the agent offers a model is MCP: a device's and the cloud's alike.
3. **Three process roles, from the same package:**
   - `api`, for requests;
   - `agent`, the LiveKit Agents worker, for conversations;
   - `worker`, for events, schedules and notifications.

   Each grows by running more copies.
4. **Modules talk through their public APIs within a process, and through domain events across processes.** A module writes an event in the same transaction as its change (a transactional outbox), and `worker` delivers it through a queue.
5. **Data:** PostgreSQL is the source of truth, with pgvector for embeddings. Redis holds only what can be lost: caches, rate limits, presence, the queue. Object storage holds backups and exports.
   - Each module's data is its own: others refer to it by identifier, never by a foreign key or a join, so that a module can move to a database of its own.
   - Everything that grows with use carries its home ([ai-platform ADR-0005](https://github.com/trungnguyen278/ai-platform/blob/main/docs/decisions/0005-homes.md)), and high-volume tables are partitioned by time.
6. **Cost is part of the architecture.**
   - Every paid stage is a chain of providers, official free tiers first, with fallback.
   - Every call is metered, against daily quotas per device and per owner.
   - When a budget runs out, the answer degrades, never to silence.
   - Unofficial endpoints may serve development, never a release.
7. **One host to start.** Docker Compose runs every process and store, and the same images run on a larger host later.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Microservices, one per module | A network, deployments and data consistency between every pair of modules, for one person. The boundaries, enforced inside one codebase, keep the option to extract a module later. |
| Luni Cloud's shape, a server and an AI service without boundaries | It gave no rule for where code belongs. A new device kind or provider would touch everything; the user called it a preview. |
| Serverless functions, or a backend as a service (Firebase, Supabase) | A conversation is a long-lived, real-time job in a LiveKit room, not a request. Prime Video moved a comparable media workload off functions for cost. The cloud's core would also be tied to one vendor. |
| Redis as the store for memory (Luni Cloud) | Memory must last and be deletable per owner (PRIV-4). One durable database, with pgvector, keeps it with the rest of the owner's data. |

## Consequences

- Positive:
  - a new device kind, skill, language or provider is a new adapter or module, not a rewrite;
  - one deployment, cheap to run, which grows by copies;
  - the boundaries are checked in CI, so they hold as the code grows;
  - costs are visible and bounded by design.
- Negative / accepted trade-offs:
  - the boundaries need discipline, and an import checker in CI;
  - one codebase means one release for all modules;
  - extracting a module into its own service later still takes work, though not a redesign.
- Follow-up:
  - ADR-0002, the stack;
  - ADR-0003, the conversation pipeline: providers, Vietnamese end of speech, the budget's numbers;
  - ADR-0004, hosting and LiveKit's media;
  - ADR-0005, owners' sign-in;
  - ADR-0006, personal data and the law;
  - the app's API, defined in ai-platform.

## References

Retrieved 2026-10-28:

- Shopify Engineering: [Deconstructing the Monolith](https://shopify.engineering/deconstructing-monolith-designing-software-maximizes-developer-productivity) (2019), and [its follow-up](https://shopify.engineering/shopify-monolith).
- [Prime Video swaps microservices for a monolith, cutting costs by 90%](https://www.infoq.com/news/2023/05/prime-ec2-ecs-saves-costs/), InfoQ, 2023, on Prime Video Tech's own post.
- Home Assistant: [voice pipelines](https://developers.home-assistant.io/docs/voice/pipelines/).
- [xinnan-tech/xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server).
- [import-linter](https://github.com/seddonym/import-linter): "Lint your Python architecture".
- [The transactional outbox pattern](https://microservices.io/patterns/data/transactional-outbox.html), microservices.io.
- [Luni_Cloud](https://github.com/trungnguyen278/Luni_Cloud): its architecture and its AI module, as of 2026-07-04.
