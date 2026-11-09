# Pinned CI tools

CI's tools run at pinned versions and come from the Actions cache, so a run makes no request to
`github.com/.../releases` (or the uv mirror) once the cache is warm. Release downloads returned 504
several times a day and failed jobs before any test ran (#384). A pin only moves when someone bumps
it: nothing here updates on its own.

## Where each version lives, and who bumps it

| Tool | Version in | Dependabot |
|---|---|---|
| Bun | `.bun-version` (read by `oven-sh/setup-bun` via `bun-version-file`) | **No.** Dependabot does not bump `.bun-version`. Bump by hand. |
| Node (E2E stack) | `.node-version` (read by `.github/actions/stack`) | **No.** Dependabot does not bump `.node-version`. Bump by hand. |
| uv | `UV_VERSION` in `.github/actions/tools/action.yml` | **No.** Bump by hand. |
| gitleaks | `GITLEAKS_VERSION` + `GITLEAKS_SHA256` in `.github/actions/tools/action.yml` | **No.** Bump by hand, both lines. |
| actionlint | `ACTIONLINT_VERSION` + `ACTIONLINT_SHA256` in `.github/actions/tools/action.yml` | **No.** Bump by hand, both lines. |
| Python 3.10 (not in the runner image) | the `python-version` matrix in `ci.yml`; the patch is whatever setup-python downloaded on the first miss for that runner image | **No.** A new runner image (the cache key names it) downloads the current 3.10.x once. |
| The actions themselves (`setup-bun`, `setup-uv`, `setup-node`, `setup-python`, `cache`) | `uses:` refs in the workflows and `.github/actions/*` | **Yes**, the weekly `github-actions` group in `.github/dependabot.yml`. This bumps the action, not the tool version it installs. |

## How to bump

- Bun / Node: edit `.bun-version` / `.node-version` to an exact version (`1.4.5`, `26.14.0`, no
  ranges: a range makes the action look the version up online every run, which is the problem this
  removes). Node must satisfy the `engines.node` range of `OPENCLAW_VERSION` in `e2e/versions.env`;
  the stack fails with "bump .node-version" when it doesn't, so bump it in the PR that moves
  `OPENCLAW_VERSION`.
- uv: edit `UV_VERSION`.
- gitleaks / actionlint: edit the version and the sha256 of the `linux_x64` / `linux_amd64`
  tarball, from the release's `checksums.txt`
  (`gitleaks_<v>_checksums.txt`, `actionlint_<v>_checksums.txt`).
- The cache key carries the version, so the first run after a bump downloads once and saves;
  every later run is a cache hit.

## The one per-run lookup left

The compat watch's `latest` lane (`openclaw_version: latest`) resolves the newest Node in that
openclaw release's range from `nodejs.org/dist/index.json` on each daily run: it exists to run what a
user installing `openclaw@latest` gets today, Node included. Every other lane uses `.node-version`.
