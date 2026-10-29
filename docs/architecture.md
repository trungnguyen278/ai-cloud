# Architecture

> **Status:** accepted in [ADR-0001](decisions/0001-architecture.md) (the shape) and [ADR-0002](decisions/0002-stack.md) (the stack). Nothing is built yet.

The cloud is the AI of every device in the project, and runs what a fleet of devices needs: their identity, their owners, set-up, updates, operations. The robot is the first device; more kinds will come, and more owners, devices and languages. The cloud must grow with them without being rebuilt, while one person runs it on little money. Its first owner is its maker ([Phases](#phases)).

## Principles

1. **Modules with boundaries, in one codebase** (a modular monolith, as Shopify's). Each module owns its data and offers a public API. No module reaches into another's internals, and CI checks it.
2. **Ports and adapters, as on the robot.** Every outside service (AI providers, LiveKit, storage, push, weather…) sits behind a port. Configuration chooses the adapters, and simulated ones serve tests and running without accounts.
3. **Contracts are defined once, in ai-platform:** the device link, set-up, the app's API.
4. **Cost is a design constraint.** Every call to a paid service is metered, bounded and replaceable ([Budget](#budget-ai-on-little-money)).
5. **A device is the authority on itself.** The cloud acts on it only through the capabilities it declares, within its owner's safety classes.
6. **Privacy by design.** No audio is kept unless the owner asks. Logs and metrics carry no content (PRIV-5). The owner can see and delete what the cloud keeps (PRIV-4).
7. **One machine to start, more of the same to grow.** Every part runs on one host, and scales by running more copies, not by changing code.

## The big picture

```
                 ┌──────────────────────────────── ai-cloud ────────────────────────────────┐
 device ─WebRTC─▶│ LiveKit ◀──▶ agent    conversation · memory · skills                      │
 device ─HTTPS──▶│ api                   accounts · devices · updates · operations           │──▶ providers
 app ───HTTPS───▶│ api                                                                       │    (adapters):
 app ◀──push─────│ worker                events · schedules · notifications                 │    AI, weather,
                 │              PostgreSQL (+pgvector) · Redis · object storage             │    push, …
                 └──────────────────────────────────────────────────────────────────────────┘
```

Devices keep a control channel to `api` at all times, a WebSocket, and join a LiveKit room only to talk, one for each conversation (the cloud link 3.0, [ai-platform ADR-0006](https://github.com/trungnguyen278/ai-platform/blob/main/docs/decisions/0006-two-channels.md)):
- every call to a device goes through `devices`, on its control channel, whoever makes it: the agent, a reminder, the app;
- a conversation's turns and answers go between the agent and the device, in the room, with the audio.

In phase one LiveKit is LiveKit Cloud ([ADR-0004](decisions/0004-hosting.md)).

## Process roles

The same package runs as three processes, each started by its own entry point:

| Process | What it runs | Grows by |
|---|---|---|
| `api` | FastAPI: devices' control channels and tokens, set-up's registration, the app's API, administration | more replicas: it keeps no state |
| `agent` | A LiveKit Agents worker: one job per conversation, in its room | more workers, among which LiveKit spreads the jobs |
| `worker` | Background work: delivering domain events, schedules (reminders, rollouts, memory upkeep), notifications | more workers on the queue |

## Modules

| Module | Owns |
|---|---|
| `accounts` | Homes and their members: people with roles, and profiles for those who never sign in; sign-in; invitations ([ai-platform ADR-0005](https://github.com/trungnguyen278/ai-platform/blob/main/docs/decisions/0005-homes.md)) |
| `devices` | The registry: each device's key, kind, model and software, its home, set-up's registrations, its tokens, its health. Each device's session on its control channel, the one way to call its tools. The control channel is a port: a WebSocket today, other kinds for devices that cannot keep one |
| `conversation` | Conversations, the speech pipeline, turns, the persona for each kind of device and language, the tools of a turn, barge-in, fillers, confirmations |
| `memory` | What a conversation remembers, summaries across conversations, long-term facts with their embeddings; retention and deletion |
| `skills` | The cloud's own tools: time and the lunar calendar, weather, timers and reminders, knowledge, and more as they come. Each is an MCP server, run in the process or apart |
| `updates` | Release channels, rollouts in stages, announcements to devices, each device's update state |
| `operations` | Health and metrics from devices, usage and cost metering, quotas, administration |
| `notifications` | Push to the app, and email |
| `shared` | Configuration, identifiers, errors, JOSE, the protocols' schemas read from `platform/`, observability |

Each module has the same shape:

```
<module>/
├── api.py        what other modules may call: the only import they may make
├── domain/       its rules and types, without I/O
├── ports/        what it needs from outside
├── adapters/     the ports' implementations: real services, and simulated ones
├── service.py    its use cases, joining the domain and the ports
└── db/           its tables, in its own Postgres schema
```

## How modules talk

- **Within a process**, through each other's `api.py`.
- **Across processes, and for what follows a change**, through domain events. A module writes its event in the same transaction as the change (a transactional outbox), and `worker` delivers it through a queue. When `devices` records a binding, for example, `notifications` tells the app and `updates` starts checking the device's version.
- **In a conversation**, the agent gives the model one list of tools, all of them MCP: the device's, which `devices` calls on the device's control channel, and the skills', in the process or over the network. A third party's skill would be one more MCP server.
- **Events** carry a version, and arrive at least once, so every consumer is idempotent.

## The conversation pipeline

The shape is decided here; the providers are chosen in ADR-0003.

- **Two kinds of engine, behind one port:**
  - cascaded: speech recognition, a language model, then speech synthesis, all streaming;
  - realtime: one speech-to-speech model.

  Configuration chooses the engine per language and per budget, and a session can fall back from one to the other.
- **A turn:**
  1. A wake word opens a conversation, and the device streams what it hears until the conversation ends. The cloud finds the turns in it.
  2. The cloud decides the end of speech (ai-robot ADR-0012). LiveKit's turn detector has no Vietnamese, so ADR-0003 chooses how.
  3. The cloud judges whether the turn was meant for the device, since people talk around it, and a television too. It uses the Live model's proactive audio where the model has it (Gemini 3.1 Flash Live does not), or a check of what was said. A turn that was not meant for the device is dropped with `turn.cancel`, and a conversation that holds nothing else is ended (ai-robot ADR-0018, proposed).
  4. A router answers what it can without a model (the time, a timer, a status), sends chat to a small, fast model, and knowledge to a stronger model or a skill.
  5. Tools run: the device's capabilities and the cloud's skills.
  6. The answer streams as speech, with expressions and moods for the device.
  7. After the turn, `worker` decides what to remember.
- Fillers, confirmations, barge-in and memory in tiers come from Luni Cloud's Agent Brain v2, and live in `conversation`'s domain.

## Budget: AI on little money

- **Chains of providers.** Each stage of the pipeline has an ordered chain of providers, official free tiers first, with health checks and fallback, configured per language. [providers.md](providers.md) lists the free tiers, checked every three months.
- **Official interfaces only, in a release.** Scraped pages and unofficial endpoints (Luni Cloud used Google's AI Mode through a browser, edge-tts, gTTS and Google's Web Speech) break without notice and breach their terms. They may be adapters for development, never in a release's configuration.
- **Metering.** Every call records its provider, its units (tokens, seconds of audio, characters) and their estimated cost.
  - Each device and each owner has daily quotas.
  - When a budget runs out, the chain moves to the next free option, or the answer degrades: shorter, or the face alone. It is never silence.
- **Caching:** speech for fixed phrases (fillers, confirmations), embeddings, and the skills' results (weather), each for its time.
- **Cheap first.** Rules and a small model decide most turns; a strong model works only when a turn needs knowledge.

## Data

- **PostgreSQL** is the source of truth: one schema per module, migrations per module, and pgvector for the embeddings of `memory`.
- **Each module's data is its own.** Other modules refer to it by identifier only: no foreign keys or joins across schemas, and reads go through the owner's `api.py`. A module's tables can then move to a database of their own.
- **Everything that grows with use carries `home_id`** ([ai-platform ADR-0005](https://github.com/trungnguyen278/ai-platform/blob/main/docs/decisions/0005-homes.md)), so that the data can be partitioned or sharded by home. High-volume tables (usage, health reports, turns) are partitioned by time, and their old partitions dropped as their retention says.
- **Redis** holds only what can be lost: caches, rate limits, presence, the event queue (Redis Streams). Losing it loses nothing that matters.
- **Object storage**, S3-compatible, holds backups and exports. Audio is not kept unless an owner asks.
- **Retention:** each table says how long it keeps its rows, and an owner's data can be deleted (PRIV-4).

## Interfaces

All are defined in ai-platform, which the cloud pins at `platform/`:

| With | How |
|---|---|
| Devices | The cloud link: a control channel always on (a WebSocket), and a LiveKit room to talk ([ai-platform ADR-0006](https://github.com/trungnguyen278/ai-platform/blob/main/docs/decisions/0006-two-channels.md)) |
| The app | The app's API over HTTPS (to be defined), and push |
| Set-up | The registration that the app relays (the set-up protocol) |

## Security

- **Devices** prove themselves with keys in their TPMs, which buy short-lived tokens for their control channel (ai-robot ADR-0010). Each conversation's room has a token of its own. The cloud acts within the safety classes they enforce.
- **Owners** sign in as their own ADR decides. Every request is confined to the account and its devices (SEC-6).
- **Secrets** come from the environment or a secret store, and are never logged.
- **Limits** on rates and sizes at the edge (SEC-15) also bound what an abuser can cost.
- **Keys rotate.** The keys that sign rooms' tokens, and those that verify sessions, are replaced on a schedule, and the old ones honoured until their tokens expire.
- **The law.** Personal data follows the law where the owners live, Vietnam's first. ADR-0006 settles it before the first real users. The products are for adults, not directed to children (PRIV-7).

## Observability

- Structured logs, without content.
- Metrics for each turn's latency (from the end of speech to the first sound), and for each provider's errors and cost.
- Traces across `api`, `agent` and `worker`, with OpenTelemetry.
- Objectives, set before launch: the share of turns answered within a target latency, and the cloud's availability.

## Deployment

- **Containers:** `api`, `agent`, `worker`, the LiveKit server, PostgreSQL, Redis and a reverse proxy.
  - Docker Compose runs them on one host to start, as Luni Cloud did.
  - The same images run later on a rented server or on Kubernetes.
- **Phase one** runs on a machine at home, reached through a Cloudflare Tunnel (HTTPS and WebSockets only), with media through LiveKit Cloud's free plan. No port opens at home ([ADR-0004](decisions/0004-hosting.md)). Devices at home would talk through a LiveKit server at home instead, and reach the cloud on the home network: proposed in [ADR-0007](decisions/0007-media-at-home.md).
- **Environments:** development, with simulated providers and no accounts; production.

## Phases

The architecture is for many owners; the cloud is built for one first, its maker, with one home and one robot. It opens to others once its functions are complete. Each phase builds part of the same design, and nothing that a later phase replaces:

- **From the start:** whatever is costly to add later. That means the data's model (homes, people and roles), identity (accounts and devices' keys), the protocols and their versions, ports and adapters, and privacy by design.
- **When others join:** what only many owners need.

| | Phase one: the maker alone | When others join |
|---|---|---|
| Homes and people | One home and its owner. Members and roles are in the data from the start. | Invitations, roles in the app, several homes |
| Sign-in | The owner's account, through the same sign-in others will use (ADR-0005) | More accounts |
| Budget | One daily budget for the whole cloud, a guard against runaway costs | Quotas per home and per device |
| Hosting | One host (ADR-0004) | The [growth](#growth) table |
| Data and the law | The owner's own data | ADR-0006 settled and applied before the first other owner: consent, retention, deletion, export |
| The cloud's releases | Deployed when the owner is ready | Feature flags and releases in stages |
| Operations | Logs and metrics | Alerts, an administration console, support |

## Growth

How each part grows, should the project grow a lot. None of it changes a module's code: each step is a new adapter, a deployment, or a split along a boundary that already exists.

| Part | To start | When it grows |
|---|---|---|
| `api` | One replica | More replicas, behind the proxy |
| `agent` | One worker | More workers, among which LiveKit spreads the jobs |
| LiveKit | One server | A cluster, then servers in several regions. Each conversation's start already names the server the device joins, so it can choose by region. |
| PostgreSQL | One database, a schema per module | A connection pooler and read replicas; a module's schema moved to a database of its own; then tables sharded by home |
| Events | Redis Streams | NATS or Kafka, behind the same port |
| Memory's search | pgvector | A vector store of its own, behind `memory`'s port |
| AI providers | Official free tiers, metered | Paid tiers chosen by measured unit cost; models of our own, once volume pays for them; a home's own API keys, if its owner brings them |
| Skills | In the process | MCP servers apart, then third parties' |
| Devices | One kind, on a WebSocket | More kinds, each declaring its capabilities; control channels of other kinds for devices that cannot keep a WebSocket |
| The cloud's releases | One image | Feature flags by home or device, so that a change rolls out in stages |
| The team | One person | Each module with its owners (CODEOWNERS), its tests and its migrations; a module that must scale apart becomes a service along its boundary |

## Repository layout

```
ai-cloud/
├── platform/            ai-platform, pinned
├── src/ai_cloud/
│   ├── shared/
│   ├── accounts/  devices/  conversation/  memory/
│   ├── skills/  updates/  operations/  notifications/
│   └── entry/           api.py · agent.py · worker.py
├── migrations/          one history per module
├── config/              base.toml, and one file per environment
├── tests/               per module, and end to end against a simulated device
├── deploy/              Compose files and container images
└── docs/
```

## From Luni Cloud

Luni Cloud was the preview. What it taught, and what changes:

| Luni Cloud | Here |
|---|---|
| One AI service behind HTTP endpoints (`/chat`, `/stt`, `/tts`) | A streaming pipeline in a LiveKit Agents worker |
| The robot on a WebSocket, with a device token and its MAC address | The cloud link 3.0: a WebSocket for the session, WebRTC only to talk; keys in a TPM, short-lived tokens |
| Memory in Redis Stack | PostgreSQL with pgvector; Redis only for what can be lost |
| Free tiers, including unofficial endpoints, in fallback chains | Chains of official free tiers, metered and bounded |
| Agent Brain v2: router, fillers, confirmations, barge-in, memory in tiers | Kept, in `conversation`'s domain |
| Docker Compose behind a Cloudflare Tunnel | Kept, for HTTPS and the control channel; media through LiveKit Cloud (ADR-0004) |

## Decisions

| ADR | Decides |
|---|---|
| [0001](decisions/0001-architecture.md) | This architecture |
| [0002](decisions/0002-stack.md) | The stack: Python, LiveKit Agents, FastAPI |
| [0003](decisions/0003-conversation-pipeline.md) | The conversation pipeline: Gemini Live first, a chain of free tiers behind it |
| [0004](decisions/0004-hosting.md) | Hosting: one machine at home behind a tunnel, LiveKit Cloud for media |
| ADR-0005 | Owners' sign-in |
| ADR-0006 | Personal data and the law |
| [0007](decisions/0007-media-at-home.md) | Media at home: devices at home talk through a LiveKit server at home (proposed) |
