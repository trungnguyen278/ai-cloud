# ai-cloud

> **Status:** new. The repository's conventions are in place; there is no code yet. The first decisions come next: the stack, the conversation pipeline, storage and hosting ([dev log](docs/devlog/2026-10.md)).

The cloud side of the project's AI devices, the first of which is [ai-robot](https://github.com/trungnguyen278/ai-robot), a mobile robot with an expressive face that people talk to. A device handles its own body, screen and senses; the cloud is its AI, and runs what every device needs from it.

## What it does

The device ⇄ cloud link and its requirements are defined in [ai-platform](https://github.com/trungnguyen278/ai-platform) (`protocols/cloud/`), with the decisions behind them.

- **Conversations.** It joins each device's LiveKit room as the agent, hears the user's turns, decides when they have finished, and answers with speech, in the language spoken to it (ai-platform ADR-0002).
- **Capabilities.** It drives what each device offers, such as a robot's expressions, scenes and motion, through the MCP tools the device declares, within the safety classes its owner allows (SEC-8, SEC-9).
- **Identity.** It knows each device's public key, and gives a device that proves itself the tokens for its room.
- **Set-up and ownership.** It accepts the registration that the owner's app relays during set-up, and binds the device to the owner's account.
- **Updates.** It announces releases to devices, and can hold one back or roll it out in stages.
- **Operations.** It sees each device's health and update state, without audio, images or conversations (OPS-1, PRIV-5).

## Repository layout

```
ai-cloud/
├── docs/        Decision records (ADRs) and the dev log
└── .claude/     Task checklists for AI assistants (see AGENTS.md)
```

`platform/` comes with the pin of ai-platform, and the code's layout with the decision on the stack.

## Documentation

- [docs/](docs/README.md) — the cloud's own decisions and its dev log
- [ai-platform](https://github.com/trungnguyen278/ai-platform) — the protocols, and the decisions that span the repositories
- [CONTRIBUTING.md](CONTRIBUTING.md) — branches, commits, documentation, secrets, licensing

## Related repositories

| Repository | What it is |
|---|---|
| [ai-robot](https://github.com/trungnguyen278/ai-robot) | The robot: hardware, software, face, firmware |
| [ai-platform](https://github.com/trungnguyen278/ai-platform) | What the repositories share: the protocols, and the decisions that span them |
| `ai-app` | The owner's app: set-up, settings, remote control (not created yet) |

## License

Released under the [MIT License](LICENSE).
