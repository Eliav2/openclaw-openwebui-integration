# Contributing

Thanks for helping out. Bug reports, fixes and docs changes are all welcome.

## Run the tests

You need [uv](https://docs.astral.sh/uv/). [bun](https://bun.sh) is only for the docs site and a few frontend tests.

```bash
git clone https://github.com/Eliav2/openclaw-openwebui-integration
cd openclaw-openwebui-integration

uv run --with-requirements requirements-dev.txt pytest -q   # unit tests, no setup needed
uvx ruff@0.17.0 check .                                      # lint, as CI runs it
python3 build.py --check-all                                 # the built files match src/
python3 scripts/magic_baseline.py                            # no new magic number or string in a comparison
scripts/lint-js/run.sh                                       # the same for JavaScript and TypeScript

bun install
bun run docs:build                                           # docs site, fails on a dead link
```

Literals that carry meaning get a name (`AGENTS.md`, "No magic numbers or strings"); CI fails on a new one.

`openclaw_pipe.py` and `openclaw_status_action.py` are built from `src/openclaw_pipe_pkg/`. Edit the source there, then run `python3 build.py --all`.

The end-to-end suite runs a real Gateway and Open WebUI in CI on every pull request. To run it locally, and for everything else (CI jobs, screenshots, the docs site), see [Development](https://eliav2.github.io/openclaw-openwebui-integration/development).

## Report a bug or ask for a feature

[Open an issue](https://github.com/Eliav2/openclaw-openwebui-integration/issues/new/choose) and pick a form. For a bug, the versions and the `[openclaw-pipe]` log lines usually say more than anything else. [Troubleshooting](https://eliav2.github.io/openclaw-openwebui-integration/troubleshooting) says where the logs are.

Security problems go through [SECURITY.md](./SECURITY.md), not a public issue.

## Open a pull request

1. For anything bigger than a small fix, open an issue first so we can agree on the approach.
2. Branch from `main` and keep the PR to one change.
3. Add or update a test for what you changed, and update the docs if users see it.
4. Make sure the commands above pass. CI runs them too, plus the end-to-end suite.
5. If the PR has a visual change (something on screen in the chat, the Status dialog or the composer), show the Before and After in the description as a 2-column table, one row per change, with one short heading above each table saying what changed. A change with nothing on screen shows no images. The pull request template has the format and `AGENTS.md` the upload steps.

By contributing you agree that your work is released under the [MIT License](./LICENSE).
