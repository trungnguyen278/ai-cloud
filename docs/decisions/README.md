# Architecture decision records (ADRs)

Each significant decision is a short file: context, decision, alternatives considered and consequences — so that years from now we still know **why**.

- Template: [0000-template.md](0000-template.md). How to write one: the `adr` checklist (`.claude/skills/adr/SKILL.md`).
- Never edit an old ADR to change a decision; write a new one and mark the old one "Superseded by …".
- These are the cloud's own decisions. A decision that spans repositories, such as a protocol, is recorded in [ai-platform's decisions](https://github.com/trungnguyen278/ai-platform/tree/main/docs/decisions); cite it as "ai-platform ADR-NNNN".

| # | Decision | Status | Date |
|---|---|---|---|
| [0001](0001-architecture.md) | The architecture: a modular monolith, with ports and adapters | Proposed | 2026-10-28 |
| [0002](0002-stack.md) | The stack: Python, with LiveKit Agents | Proposed | 2026-10-28 |
| [0003](0003-conversation-pipeline.md) | The conversation pipeline: Gemini Live first, a chain of free tiers behind it | Proposed | 2026-10-28 |
