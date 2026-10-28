# Contributing

These conventions apply to everyone working in this repository — people and AI assistants alike. They match the project's other repositories, so that it works the same way everywhere. This is a long-running project: consistency matters more than speed.

## Design rules

1. **The protocols are defined once**, in [ai-platform](https://github.com/trungnguyen278/ai-platform), which this repository pins as a git submodule at `platform/`. Code generates from it or reads it; never hand-copy definitions. A change to a protocol starts there.
2. **A device is the authority on itself.** The cloud acts on a device only through the capabilities the device offers, and within the safety classes its owner allows (SEC-8, SEC-9). Physical safety never depends on the cloud (SEC-10).
3. **Each device's data is its owner's.** Every request is confined to the device that made it and the account it is bound to (SEC-6). The owner can see and delete what the cloud keeps (PRIV-4), and health reports and logs carry no audio, images or conversations (PRIV-5).
4. **Never hard-code a product's name.** Repositories, packages and identifiers use neutral, descriptive names (ai-platform ADR-0001).
5. **Build for the long run.** Choose the design that will last, even when it is more work: effort is no reason to settle for less. Do not build stopgaps meant to be replaced later. When the lasting choice is not clear yet, settle it first, with research, measurements or an ADR. Work may still land in steps, as long as each step belongs to the final design. Simulated devices and services are not stopgaps: they stay, for tests and for running without hardware.

## Portability

The repository must work for anyone who clones it, on any machine.

- No person- or machine-specific values in committed files: absolute paths, user names, addresses, credentials, editor settings. Read them from environment variables or a git-ignored local file, and document them in `.env.example`.
- Everything the cloud needs to run is described in the repository: its dependencies, its configuration, how to start it locally.
- Scripts state the operating system and tools they need; prefer cross-platform tooling.

## Language

Everything in the repository — code, comments, documentation and commit messages — is written in English. What people read in the products comes from catalogs, in their language (ai-platform ADR-0002).

## Branches

- `main` must always be usable. Do not commit to it directly: work on a branch and merge through a pull request.
- Branch names follow `<type>/<short-description>`, e.g. `feat/device-tokens`, `fix/turn-timeout`, `docs/adr-stack`, `chore/ci`.

## Commits

Follow [Conventional Commits](https://www.conventionalcommits.org/): `<type>(<scope>): <short summary in the imperative mood>`.

| Type | Use for |
|---|---|
| `feat` | A new feature |
| `fix` | A bug fix |
| `docs` | Documentation only |
| `refactor` | Restructuring without behavior change |
| `test` | Adding or changing tests |
| `chore` | Tooling, configuration, CI, housekeeping |

The scope is the affected area: `agent`, `auth`, `accounts`, `updates`, `ops`, `platform` (moving the pin), `docs`, `ci`. Add a scope when a new area appears.

Example: `feat(auth): give devices their tokens for their room`

Keep commit messages short. Every commit stays in the history for good, and the diff already shows what changed.

- **The subject** says what changes, in under 72 characters.
- **A body** is optional: at most a few lines on *why*, when the subject does not say it.
- **No inventories.** Leave out lists of files, settings or tests, and test counts.
- **No review narratives.** What a review found belongs in the conversation or the dev log, not in the commit.

## Pull requests

- A pull request carries **exactly one commit**, written as a Conventional Commit. To change it, amend that commit (`git commit --amend`) and update the branch with `git push --force-with-lease`; do not push follow-up commits.
- A branch that already has several commits is squashed locally before pushing: `git reset --soft $(git merge-base origin/main HEAD)`, then commit once.
- Pull requests are merged with a merge commit, the only merge method enabled on GitHub, so each pull request adds one merge commit and its single commit to `main`.
- The description is short, following the template:
  - a few sentences on what changes and why;
  - one line on how it was tested;
  - the checklist.

## Documentation travels with the change

Checklists for these steps live in `.claude/skills/<name>/SKILL.md`.

- Protocol changes → ai-platform first; then the pull request that implements them here moves the submodule's pin.
- Significant technical decisions → an ADR (`adr` checklist): here for the cloud alone, in ai-platform for decisions that span repositories. Anything not explicitly agreed stays `Proposed`.
- End of a work session → an entry in `docs/devlog/` (`devlog` checklist).

## Secrets and large files

- Never commit API keys, signing keys, tokens or passwords. Use `.env` (git-ignored) or the deployment's secret store, and commit a `.env.example` without real values.
- Secrets are never logged, shown or sent to a third party's service beyond what it needs.
- Do not commit large binaries (models, recordings, dumps). Use Git LFS or GitHub Releases and link them from the relevant README.

## Facts and sources

- What a service, a model or a library offers — features, limits, languages, prices, versions — needs a source; otherwise mark it **unverified**.
- Prices and versions are always dated.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).

- New source files start with an SPDX header, such as `# SPDX-License-Identifier: MIT` or `// SPDX-License-Identifier: MIT`.
- Third-party code, models and assets keep their original licenses. List them in `THIRD_PARTY_NOTICES.md` when the first one is added.
