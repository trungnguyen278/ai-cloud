---
name: adr
description: Record an architecture or design decision as an ADR (Architecture Decision Record) in docs/decisions/. Use when a significant technical choice is made, revisited or reversed — the language and framework, the conversation pipeline and its models, storage, hosting, authentication — or when the user asks to write down a decision or to explain why something was chosen.
---

# Recording a decision (ADR)

1. Decide where it belongs:
   - the cloud alone: here, in `docs/decisions/`;
   - more than one repository, such as a protocol, or something a device or the app must do: in ai-platform's `docs/decisions/`, with its own `adr` checklist. Link it from here.
2. Read `docs/decisions/README.md` to get the next number and check for related ADRs, here and in ai-platform.
3. Copy `docs/decisions/0000-template.md` to `docs/decisions/NNNN-short-title.md` (four digits, kebab-case).
4. Fill in Context → Decision → Alternatives considered (with why each was rejected) → Consequences (positive and negative) → References. Keep it short; include numbers and sources where available, dated.
5. Status: `Proposed` while under discussion, `Accepted` only once the people responsible for the decision have explicitly agreed. Never accept a decision on your own.
6. To change an earlier decision, **do not rewrite the old ADR**: set its status to `Superseded by NNNN` and write a new one.
7. Add a row to the table in `docs/decisions/README.md`.
8. Commit with the scope `docs`, e.g. `docs(decisions): add ADR-0001 on the stack`.
