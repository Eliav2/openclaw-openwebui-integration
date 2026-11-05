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
3. **The README's "What you get":** only when the feature is headline-level, as a bullet linking to its page on the docs site. The README stays short: details live on the site (`docs/`), and a new page goes in the site's sidebar in `docs/.vitepress/config.mts` (see [Development](docs/development.md#the-docs-site)).
4. **Its screenshot:** a scene in `scripts/screenshots/scenes.json` and `capture.py`, taken by the Screenshots workflow, committed to `docs/img/` and shown next to its description as `<img src="..." alt="..." width="...">`, never `![alt](path)`: github.com shows an image without a width across the whole page. Width by kind: a phone shot 320, a strip (3x wider than tall) half its pixel width, a wide desktop shot half its pixel width up to 800. A feature with nothing on screen shows real log or command output as a code block instead.
5. **`docs/versions.md`:** when the feature needs a newer Open WebUI or OpenClaw than the minimums.

A PR that changes how something already documented behaves (a fix included) corrects the text and images that no longer hold, in the same PR. A stale doc is a bug in that PR, not a follow-up.

One screenshot per scene, in the light theme: no dark copies, in the docs or anywhere else.

A PR with a visual change shows its Before and After in its description as a 2-column table, one row per change, light theme, from the Screenshots workflow, with one short heading above each table saying what changed. Put every row of one change in that table; a second change gets its own heading and its own table, never a heading between rows (it would end the table):

```markdown
### Notification shows the reply text
| Before | After |
|---|---|
| <img src="URL" alt="..." width="320"> | <img src="URL" alt="..." width="320"> |
```

The same table is the format for Before/After images in docs and reports. When the change unfolds over time (streaming, the queue, Stop, cards, the drawer), the After may be one light GIF from the Demos workflow (a demo in `scripts/demos/demos.json`) instead of a still; the Before stays one still. A static change keeps the still pair, and a backend change shows no images. Upload the images with `gh pr create --attach` or `gh pr edit <n> --attach` so the description shows GitHub asset URLs; never commit Before/After images, push them to a side branch, or link a branch's `raw.githubusercontent.com` URL (it breaks when the branch is deleted). gh rewrites only `![alt](./file.png)` references, not the `src` of an `<img>` tag (checked on gh 2.102.0: a relative `src` stays relative, and an attached file the body does not reference as markdown is appended as a markdown image). So: write the body with `![before](./before.png)` and `![after](./after.png)`, run `gh pr edit <n> --attach ./before.png --attach ./after.png`, read the body back (`gh pr view <n> --json body --jq .body > body.md && [ -s body.md ]`), turn each uploaded `![alt](URL)` into `<img src="URL" alt="alt" width="320">` inside the table, and save it with `gh pr edit <n> --body-file body.md`. Do this with `&&` and check the file is not empty before the edit: a failed read in a chain once blanked a PR body. Docs images stay committed in `docs/img/`; a PR that changes one attaches it too instead of linking the branch copy.

The PR media check (`.github/workflows/pr-media.yml`, logic and file list in `scripts/pr_media.py`) enforces this: a PR whose diff touches a UI source (the Status dialog and drawer, the composer, the turn writer) or `docs/img/` fails until its description has an image (an `<img>` tag or an inline markdown image `![alt](https://...)`; a reference-style image `![alt][ref]` is not recognised; the check does not look at the table or its heading, it asks for any image). A change to one of those files that nothing on screen shows gets the label `no-visual-change` instead; a PR labelled `prose-only` passes too, since the Prose only check proves its code unchanged. The check runs again when the description or the labels change. To show a Before for code that is already merged, run the Screenshots workflow on a branch with the old code (`gh workflow run screenshots.yml --ref <branch> -f only=<scenes>`). Its description shows images as `<img>` tags or markdown `![alt](url)` images; `gh --attach` uploads them as user-attachments assets.

A feature that is best seen moving can also show a demo GIF on its page: a demo in `scripts/demos/demos.json`, recorded by the Demos workflow into `docs/img/demos/` (light theme, 960 px wide, at most 15 s and 1 MB), shown as an `<img>` at most 800 wide.

Every PR description has a `Docs:` line: what it updated, or "no change needed" and why, so the reviewer can check the claim.

A PR that removes a feature removes its bullet, its section, its scene and its images in the same change, as the no-legacy rule says.

`tests/test_docs_unit.py` fails on a broken link, an unused or oversized image or demo GIF, an image without a width or wider than its kind allows, an em dash, or a tested version that differs from `e2e/versions.env`. It cannot tell whether a feature is documented: reviewers check that against the list above.

## User flows have a browser test

A PR that adds a user flow (something a user does in the chat, the Status dialog or the composer) adds its test to `e2e/test_browser_flows.py` in the same PR: Playwright on the E2E stack, asserting on what the user sees (DOM state), waiting on state and never on time, with no retries. A PR that changes a flow keeps its test passing. These tests run on GitHub runners only, in the E2E job.

## No magic numbers or strings

A literal that carries meaning gets a name: a timeout, a count, a limit, a status code, an event type, a role, a state word. Comparing against `3`, `200`, `"final"` or `"assistant"` inline is a finding, in a review and in CI.

- Name it where it belongs: a module constant with a comment saying what it is, an `http.HTTPStatus` member, an enum or a frozen set of the values. Not a local alias that hides the number.
- `0`, `1`, `-1` and `""` need no name.
- Python is checked by ruff `PLR2004` with strings on (`ruff-magic.toml`), through `scripts/magic_baseline.py`. JavaScript and TypeScript are checked by ESLint `no-magic-numbers` plus a string-comparison rule (`eslint.config.mjs`), through `scripts/lint-js/run.sh`. Both fail CI on a hit their baseline (`scripts/magic-baseline.json`, `scripts/lint-js/eslint-suppressions.json`) does not already hold.
- The baselines hold the hits that existed when the rule arrived. Touching a baselined line is a good moment to fix it; fixing one only shrinks the baseline (`--update`, `--suppress-all`). Never add to a baseline to get a change through.
- Tests, `e2e/` and the built `openclaw_*.py` files are not checked: a test asserts expected values, and the built files are generated from `src/`.

## Writing

- No em dashes, in docs, code comments, commit messages or PR descriptions.
- User-facing pages use the reader's terms: no issue numbers or internal names. That is `README.md` and all of `docs/` except the contributor pages (`architecture.md`, `development.md`, `metadata-contract.md`, `rehydration-persistence.md`) and `docs/design/`.

## CodeRabbit

CodeRabbit on this repo is configured by Eliav directly: do not install, configure or enable it, and do not add a `.coderabbit.yaml`.
