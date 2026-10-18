# Rules for this repo

For anyone changing this repo, people and AI tools alike, and for reviewers. Claude Code reads this file through `.claude/CLAUDE.md`; CodeRabbit reads it as this repo's code guidelines.

## No legacy paths

This repo does not carry backwards-compatibility shims or deprecation periods.

- When replacing a path, config key, or behavior: delete the old one in the *same* change that adds the new one. Do not leave both live "for now."
- No deprecation warnings, no dual-write/dual-read shims, no feature flags kept around just to ease a transition.
- If a caller (internal or external) still needs the old path, that's a reason to coordinate the change, not a reason to keep a shim.

## Docs and screenshots ship with the feature

A user-visible feature is anything a user sees, sets or runs: something in the chat or the Status dialog, a valve, an installer command or flag. A PR that adds or changes one updates, in the same PR:

1. **`docs/features/index.md`:** its bullet, linking to its section.
2. **Its page:** the section in `docs/features/` (or `docs/install/` for setup) that describes it, with its valves linked to `docs/configuration.md#<valve>`. `docs/configuration.md` is generated from the Valves classes: run `python3 build.py --all` after changing a valve.
3. **The README's "What you get":** only when the feature is headline-level. Its image then also goes in the README's "In pictures".
4. **Its screenshot:** a scene in `scripts/screenshots/scenes.json` and `capture.py`, taken by the Screenshots workflow, committed to `docs/img/` and shown next to its description as `<img src="..." alt="..." width="...">`, never `![alt](path)`: github.com shows an image without a width across the whole page. Width by kind: a phone shot 320, a strip (3x wider than tall) half its pixel width, a wide desktop shot half its pixel width up to 800. A feature with nothing on screen shows real log or command output as a code block instead.
5. **`docs/versions.md`:** when the feature needs a newer Open WebUI or OpenClaw than the minimums.

A PR that changes how something already documented behaves (a fix included) corrects the text and images that no longer hold, in the same PR. A stale doc is a bug in that PR, not a follow-up.

One screenshot per scene, in the light theme: no dark copies, in the docs or anywhere else.

A PR with a visual change shows one Before and After pair in its description, light theme, from the Screenshots workflow.

Every PR description has a `Docs:` line: what it updated, or "no change needed" and why, so the reviewer can check the claim.

A PR that removes a feature removes its bullet, its section, its scene and its images in the same change, as the no-legacy rule says.

`test_docs_unit.py` fails on a broken link, an unused or oversized image, an image without a width or wider than its kind allows, an em dash, or a tested version that differs from `e2e/versions.env`. It cannot tell whether a feature is documented: reviewers check that against the list above.

## User flows have a browser test

A PR that adds a user flow (something a user does in the chat, the Status dialog or the composer) adds its test to `e2e/test_browser_flows.py` in the same PR: Playwright on the E2E stack, asserting on what the user sees (DOM state), waiting on state and never on time, with no retries. A PR that changes a flow keeps its test passing. These tests run on GitHub runners only, in the E2E job.

## Writing

- No em dashes, in docs, code comments, commit messages or PR descriptions.
- User-facing pages use the reader's terms: no issue numbers or internal names. That is `README.md` and all of `docs/` except the contributor pages (`architecture.md`, `development.md`, `metadata-contract.md`, `rehydration-persistence.md`) and `docs/design/`.

## CodeRabbit

CodeRabbit on this repo is configured by Eliav directly: do not install, configure or enable it, and do not add a `.coderabbit.yaml`.
