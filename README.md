# ai-cloud

> **Status:** new. The skeleton runs: the `api` process answers its health check, and does nothing else yet; the token endpoint and the control channel come next. The [architecture](docs/architecture.md), the stack, the conversation pipeline and hosting are accepted; owners' sign-in and personal data are still to decide ([dev log](docs/devlog/2026-10.md)).

The cloud side of the project's AI devices, the first of which is [ai-robot](https://github.com/trungnguyen278/ai-robot), a mobile robot with an expressive face that people talk to. A device handles its own body, screen and senses; the cloud is its AI, and runs what every device needs from it.

## What it does

The device ⇄ cloud link and its requirements are defined in [ai-platform](https://github.com/trungnguyen278/ai-platform) (`platform/protocols/cloud/`), with the decisions behind them.

- **Conversations.** It joins each conversation's LiveKit room as the agent, hears the user's turns, decides when they have finished, and answers with speech, in the language spoken to it (ai-platform ADR-0002).
- **Capabilities.** It drives what each device offers, such as a robot's expressions, scenes and motion, through the MCP tools the device declares, within the safety classes its owner allows (SEC-8, SEC-9).
- **Identity.** It knows each device's public key. A device that proves itself gets a token for its control channel, and a room for each conversation.
- **Set-up and ownership.** It accepts the registration that the owner's app relays during set-up, and binds the device to the owner's account.
- **Updates.** It announces releases to devices, and can hold one back or roll it out in stages.
- **Operations.** It sees each device's health and update state, without audio, images or conversations (OPS-1, PRIV-5).

## Repository layout

```
ai-cloud/
├── src/ai_cloud/  The package: shared, then the modules, then the process roles' entry points
├── config/        base.toml, and a file per environment
├── tests/         The tests, by package
├── docs/          The architecture, decision records (ADRs) and the dev log
├── platform/      ai-platform, pinned: the protocols, and the decisions that span the repositories
└── .claude/       Task checklists for AI assistants (see AGENTS.md)
```

The code's layout is set in the [architecture](docs/architecture.md#repository-layout). Clone with `git clone --recurse-submodules`, or run `git submodule update --init` after a plain clone.

## Development

It needs [uv](https://docs.astral.sh/uv/), which installs the Python that `.python-version` names.

```sh
uv sync                                          # the dependencies, as uv.lock has them
uv run ai-cloud api --environment development    # then http://127.0.0.1:8000/health
```

The checks, as CI runs them:

```sh
uv run ruff check . && uv run ruff format --check .
uv run lint-imports                              # the layers between the packages
uv run pytest
```

The configuration is `config/base.toml`, then the environment's file in `config/environments/`, then an optional `config/local.toml` for one machine, which git ignores. Every value is required, and is checked when a process starts. The environment comes from `--environment` or `AI_CLOUD_ENVIRONMENT`; [.env.example](.env.example) lists the variables.

## Documentation

- [docs/architecture.md](docs/architecture.md) — modules, processes, the conversation pipeline, the budget, data, deployment
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
