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
4. **Its screenshot:** a scene in `scripts/screenshots/scenes.json` and `capture.py`, taken by the Screenshots workflow in light and dark, committed to `docs/img/` and shown next to its description. A feature with nothing on screen shows real log or command output as a code block instead.
5. **`docs/versions.md`:** when the feature needs a newer Open WebUI or OpenClaw than the minimums.

A PR with a visual change shows Before and After images in its description, from the Screenshots workflow.

A PR that removes a feature removes its bullet, its section, its scene and its images in the same change, as the no-legacy rule says.

`test_docs_unit.py` fails on a broken link, an unused or oversized image, an em dash, or a tested version that differs from `e2e/versions.env`. It cannot tell whether a feature is documented: reviewers check that against the list above.

## Writing

- No em dashes, in docs, code comments, commit messages or PR descriptions.
- User-facing pages use the reader's terms: no issue numbers or internal names. That is `README.md` and all of `docs/` except the contributor pages (`architecture.md`, `development.md`, `metadata-contract.md`, `rehydration-persistence.md`) and `docs/design/`.

## CodeRabbit

CodeRabbit on this repo is configured by Eliav directly: do not install, configure or enable it, and do not add a `.coderabbit.yaml`.
