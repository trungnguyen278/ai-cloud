# AGENTS.md

Guidance for AI coding assistants working in this repository. Read [README.md](README.md) for what the cloud does and [CONTRIBUTING.md](CONTRIBUTING.md) for the conventions and design rules — they apply to assistants exactly as they do to people.

This repository is one of the project's ([README](README.md#related-repositories)). The protocols between the repositories, and the decisions that span them, live in [ai-platform](https://github.com/trungnguyen278/ai-platform), which this repository pins at `platform/`.

## How to work here

- Reply in the language the user writes in; everything committed to the repository is in English.
- Check the newest file in `docs/devlog/` to see where things stand, and add an entry when a work session ends.
- Never make a decision on someone's behalf:
  - record a significant decision as an ADR: in `docs/decisions/` if it concerns the cloud alone, in ai-platform if it concerns other repositories too;
  - leave anything that has not been explicitly agreed as `Proposed`.
- Build for the long run ([design rule 5](CONTRIBUTING.md#design-rules)): propose the design that will last, even when it is bigger, rather than one to replace later. "For now", "provisional" or "until X" in a proposal is a sign to settle the choice first.
- Never state what a service, a model or a library can do from memory: its features, limits, languages, prices and versions. Look them up, cite the source, and date it.
- Keep commit messages and pull request descriptions short, as [CONTRIBUTING.md](CONTRIBUTING.md#commits) says:
  - a subject, and at most a few lines on why;
  - for a pull request, a few sentences and the template's checklist.

  Lists of files, settings and tests, and what a review found, go in the conversation or the dev log, never in git.
- **No hard-coded values.** Before writing a number, a name or a duration in code, decide where it belongs:
  - a value someone may want to tune (a timeout, a rate, a limit, a model or its parameters) goes in the cloud's configuration, with a comment. The code takes it as a required setting, with no default, so that the configuration shows what the cloud really uses;
  - a value a protocol fixes (a version, a code, a field's limit) lives in ai-platform and, once, in the code that speaks the protocol, named and pointing to the spec;
  - a fact (a constant, a published formula) stays in the code, named, with its source;
  - a value for one deployment or one machine (an address, a credential, a key) is never committed: environment variables or the deployment's secret store.
- No text that people see is written in code: it comes from a catalog, in the language asked for (ai-platform ADR-0002).

## Task checklists

Each folder in `.claude/skills/` holds one step-by-step checklist in plain Markdown (`SKILL.md`); its header says when to use it. Follow the matching checklist before changing decisions or the dev log, and the `review` checklist before handing a pull request over.
