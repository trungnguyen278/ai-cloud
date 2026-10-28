# Architecture

> **Status:** proposed in [ADR-0001](decisions/0001-architecture.md) (the shape) and [ADR-0002](decisions/0002-stack.md) (the stack). Nothing is built yet.

The cloud is the AI of every device in the project, and runs what a fleet of devices needs: their identity, their owners, set-up, updates, operations. The robot is the first device; more kinds will come, and more owners, devices and languages. The cloud must grow with them without being rebuilt, while one person runs it on little money.

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

## Process roles

The same package runs as three processes, each started by its own entry point:

| Process | What it runs | Grows by |
|---|---|---|
| `api` | FastAPI: devices' tokens, set-up's registration, the app's API, administration | more replicas: it keeps no state |
| `agent` | A LiveKit Agents worker: one job per device's session, in the device's room | more workers, among which LiveKit spreads the jobs |
| `worker` | Background work: delivering domain events, schedules (reminders, rollouts, memory upkeep), notifications | more workers on the queue |

## Modules

| Module | Owns |
|---|---|
| `accounts` | Owners, their sign-in, households, sharing a device with others |
| `devices` | The registry: each device's key, kind, model and software, its binding to an owner, set-up's registrations, its tokens, its health |
| `conversation` | Sessions, the speech pipeline, turns, the persona for each kind of device and language, the tools of a turn, barge-in, fillers, confirmations |
| `memory` | What a session remembers, summaries across sessions, long-term facts with their embeddings; retention and deletion |
| `skills` | The cloud's own tools: time and the lunar calendar, weather, timers and reminders, knowledge, and more as they come, each behind its port |
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
- **In a session**, the agent gives the model one list of tools: the device's MCP tools, over the data channel, and the cloud's skills.

## The conversation pipeline

The shape is decided here; the providers are chosen in ADR-0003.

- **Two kinds of engine, behind one port:**
  - cascaded: speech recognition, a language model, then speech synthesis, all streaming;
  - realtime: one speech-to-speech model.

  Configuration chooses the engine per language and per budget, and a session can fall back from one to the other.
- **A turn:**
  1. The device streams the user's speech once its wake word opens a turn.
  2. The cloud decides the end of speech (ai-robot ADR-0012). LiveKit's turn detector has no Vietnamese, so ADR-0003 chooses how.
  3. A router answers what it can without a model (the time, a timer, a status), sends chat to a small, fast model, and knowledge to a stronger model or a skill.
  4. Tools run: the device's capabilities and the cloud's skills.
  5. The answer streams as speech, with expressions and moods for the device.
  6. After the turn, `worker` decides what to remember.
- Fillers, confirmations, barge-in and memory in tiers come from Luni Cloud's Agent Brain v2, and live in `conversation`'s domain.

## Budget: AI on little money

- **Chains of providers.** Each stage of the pipeline has an ordered chain of providers, official free tiers first, with health checks and fallback, configured per language.
- **Official interfaces only, in a release.** Scraped pages and unofficial endpoints (Luni Cloud used Google's AI Mode through a browser, edge-tts, gTTS and Google's Web Speech) break without notice and breach their terms. They may be adapters for development, never in a release's configuration.
- **Metering.** Every call records its provider, its units (tokens, seconds of audio, characters) and their estimated cost.
  - Each device and each owner has daily quotas.
  - When a budget runs out, the chain moves to the next free option, or the answer degrades: shorter, or the face alone. It is never silence.
- **Caching:** speech for fixed phrases (fillers, confirmations), embeddings, and the skills' results (weather), each for its time.
- **Cheap first.** Rules and a small model decide most turns; a strong model works only when a turn needs knowledge.

## Data

- **PostgreSQL** is the source of truth: one schema per module, migrations per module, and pgvector for the embeddings of `memory`.
- **Redis** holds only what can be lost: caches, rate limits, presence, the event queue (Redis Streams). Losing it loses nothing that matters.
- **Object storage**, S3-compatible, holds backups and exports. Audio is not kept unless an owner asks.
- **Retention:** each table says how long it keeps its rows, and an owner's data can be deleted (PRIV-4).

## Interfaces

All are defined in ai-platform, which the cloud pins at `platform/`:

| With | How |
|---|---|
| Devices | The cloud link: a LiveKit room (WebRTC) and its data channel, and the token endpoint |
| The app | The app's API over HTTPS (to be defined), and push |
| Set-up | The registration that the app relays (the set-up protocol) |

## Security

- **Devices** prove themselves with keys in their TPMs, which buy short-lived tokens for their own room (ai-robot ADR-0010). The cloud acts within the safety classes they enforce.
- **Owners** sign in as their own ADR decides. Every request is confined to the account and its devices (SEC-6).
- **Secrets** come from the environment or a secret store, and are never logged.
- **Limits** on rates and sizes at the edge (SEC-15) also bound what an abuser can cost.

## Observability

- Structured logs, without content.
- Metrics for each turn's latency (from the end of speech to the first sound), and for each provider's errors and cost.
- Traces across `api`, `agent` and `worker`, with OpenTelemetry.

## Deployment

- **Containers:** `api`, `agent`, `worker`, the LiveKit server, PostgreSQL, Redis and a reverse proxy.
  - Docker Compose runs them on one host to start, as Luni Cloud did.
  - The same images run later on a rented server or on Kubernetes.
- LiveKit's media needs UDP, or a TURN relay. ADR-0004 decides how the host offers it, and whether LiveKit is self-hosted or LiveKit Cloud.
- **Environments:** development, with simulated providers and no accounts; production.

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
| The robot on a WebSocket, with a device token and its MAC address | The cloud link: WebRTC, keys in a TPM, short-lived tokens |
| Memory in Redis Stack | PostgreSQL with pgvector; Redis only for what can be lost |
| Free tiers, including unofficial endpoints, in fallback chains | Chains of official free tiers, metered and bounded |
| Agent Brain v2: router, fillers, confirmations, barge-in, memory in tiers | Kept, in `conversation`'s domain |
| Docker Compose behind a Cloudflare Tunnel | Kept for HTTPS; the media's path decided in ADR-0004 |

## Decisions

| ADR | Decides |
|---|---|
| [0001](decisions/0001-architecture.md) | This architecture |
| [0002](decisions/0002-stack.md) | The stack: Python, LiveKit Agents, FastAPI |
| ADR-0003 | The conversation pipeline: providers, Vietnamese end of speech, the budget's numbers |
| ADR-0004 | Hosting, and LiveKit's media |
| ADR-0005 | Owners' sign-in |
