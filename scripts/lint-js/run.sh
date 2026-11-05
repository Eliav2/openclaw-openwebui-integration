#!/usr/bin/env bash
# The no-magic-numbers-or-strings check for the repo's JS/TS (not tests, not e2e, not docs).
# Config: eslint.config.mjs at the repo root. Packages: this directory.
#   scripts/lint-js/run.sh                 CI: fails on a hit eslint-suppressions.json does not hold
#   scripts/lint-js/run.sh --suppress-all  rewrite the baseline after a burn-down
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
(cd "$here" && bun install --frozen-lockfile >/dev/null)
cd "$here/../.."
files=$(git ls-files 'gateway-plugins/*.js' 'push/*.js' 'push/*.ts' 'scripts/*.ts' 'scripts/*.js' | grep -v -E '\.test\.')
# shellcheck disable=SC2086
exec "$here/node_modules/.bin/eslint" --suppressions-location scripts/lint-js/eslint-suppressions.json "$@" $files
