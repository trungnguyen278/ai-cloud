# 0002 — The stack: Python, with LiveKit Agents

- **Status:** Accepted
- **Date:** 2026-10-28
- **Revised:** 2026-10-28: a device's tools are called on its control channel, as the cloud link 3.0 has it.

## Context

- [ADR-0001](0001-architecture.md) sets the shape: a modular monolith, in three process roles. This decision chooses what it is built with.
- The cloud does two kinds of work:
  - **conversations, in real time.** It joins each conversation's LiveKit room as the agent (ai-robot [ADR-0009](https://github.com/trungnguyen278/ai-robot/blob/main/docs/decisions/0009-cloud-link-webrtc.md), ai-platform [ADR-0006](https://github.com/trungnguyen278/ai-platform/blob/main/docs/decisions/0006-two-channels.md)), hears the user's turns, thinks, and answers with speech. It calls the device's MCP tools on the device's control channel, a WebSocket (ai-robot ADR-0008, the cloud link 3.0);
  - **requests, over HTTPS:** the devices' control channels; a device's tokens, for assertions signed by its key (ai-robot [ADR-0010](https://github.com/trungnguyen278/ai-robot/blob/main/docs/decisions/0010-device-identity.md)); set-up's registration (ai-robot ADR-0015); later the app's API and the announcement of updates (ai-robot ADR-0014).
- LiveKit's framework for agents, LiveKit Agents, comes in Python and in Node.js:
  - LiveKit builds Python first. In 2026 the Node.js SDK still trailed it, with features such as its MCP tools pending (LiveKit's forum);
  - `livekit-agents` 1.8.9 runs on Python 3.10 to 3.14 and offers about 80 plugins. They cover speech recognition and synthesis (Deepgram, ElevenLabs, Google, Azure, Soniox, Speechmatics…), language models (OpenAI, Anthropic, Google…), Silero VAD, a turn detector and MCP.
- The turn detector's multilingual model knows 14 languages, and Vietnamese is not one of them. How the cloud decides that a Vietnamese speaker has finished (ai-robot ADR-0012) is a question for the conversation pipeline (ADR-0003).
- MCP's Python SDK (`mcp` 2.3.0) has a client that takes any transport, so the cloud can call a device's tools over its control channel, as the cloud link defines.
- The robot's software is Python too (asyncio). Its JOSE code, its JSON Schema checks and its tests are written the way the cloud's would be.

## Decision

1. **Python**, at the newest version that every dependency supports: 3.14 as of 2026-10. CI tests it.
2. **Conversations run in a LiveKit Agents worker.** Each conversation is a job in its room. The pipeline's parts are plugins, which ADR-0003 chooses.
3. **A device's tools are called through MCP's Python SDK**, its client over the device's control channel, which the `devices` module holds, as the cloud link defines.
4. **Requests are served by an ASGI service, with FastAPI**, in the `api` process (ADR-0001).
   - The three process roles are one Python package, and share its code: the protocols' schemas, read from `platform/`; the JOSE code; the access to storage.
   - What crosses to a device or the app is defined in ai-platform. Tests check every response against those schemas; FastAPI's own description of the API follows them, never the other way round.
5. **The tooling is ai-robot's:** `pyproject.toml`, ruff and pytest. Dependencies are locked with uv (`uv.lock`), so that every deployment runs what CI tested.
6. **Configuration as in ai-robot:** a file per environment, every value required and commented; secrets from the environment or the deployment's secret store, never from a file in the repository.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Node.js, with LiveKit's agents-js | LiveKit develops Python first. agents-js has fewer plugins and trailed in features in 2026, MCP among them. The robot's code is Python. |
| Pipecat, another Python framework for voice agents | LiveKit Agents comes from the makers of the server the link runs on (ADR-0009), and LiveKit dispatches its jobs to rooms itself. |
| A realtime model's own API, with no framework (OpenAI's or Google's) | It would tie the cloud to one provider. The room, the turns and the tools would still need code of our own. LiveKit Agents has plugins for these models, so ADR-0003 can still choose one. |
| Go or Rust | LiveKit has server SDKs for them, but its agents framework is Python and Node.js only. |
| aiohttp for the requests, as the robot's screen uses it | FastAPI types each request and response, which the API's contracts need. aiohttp stays where the robot serves its page. |

## Consequences

- Positive:
  - one language for the robot and the cloud;
  - LiveKit's first SDK, with plugins for most providers, so the pipeline can choose among them without new code. A provider without a plugin, such as a Vietnamese one, gets one written in Python;
  - the protocols' schemas, read from the pin, check what the cloud sends.
- Negative / accepted trade-offs:
  - Python is slower than Go or Rust. The heavy work, the models, runs at the providers or in native code; the cloud's own code orchestrates;
  - three process roles to deploy, as ADR-0001 sets them.
- Follow-up:
  - ADR-0003: the conversation pipeline, for Vietnamese first, mixed with English, and more languages later (ai-platform ADR-0002). It includes the end of speech, without LiveKit's turn detector for Vietnamese;
  - ADR-0004: hosting, and LiveKit's media;
  - the token endpoint and the control channel first, so that a robot can reach the real cloud.

## References

Retrieved 2026-10-28:

- [`livekit-agents` on PyPI](https://pypi.org/project/livekit-agents/) (1.8.9: Python 3.10 to 3.14, its plugins), and [LiveKit Agents' documentation](https://docs.livekit.io/agents/).
- [Node.js SDK features still pending](https://community.livekit.io/t/agent-node-sdk-features-eta-endcalltool-warmtransfertask-mcp-server/1290), LiveKit's forum.
- [The turn detector](https://docs.livekit.io/agents/build/turns/turn-detector/), and [`livekit-plugins-turn-detector` 1.5.0](https://pypi.org/project/livekit-plugins-turn-detector/1.5.0/): the multilingual model's 14 languages.
- [`mcp` on PyPI](https://pypi.org/project/mcp/) (2.3.0), MCP's Python SDK.
- [FastAPI](https://pypi.org/project/fastapi/) (0.162.0) and [uv](https://pypi.org/project/uv/) (0.13.6) on PyPI.
