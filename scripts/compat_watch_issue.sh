#!/usr/bin/env bash
# Keeps the one compat-watch tracking issue in step with the latest scheduled
# run (called by .github/workflows/compat-watch.yml).
#
#   fail  open the issue, or comment on it when one is already open
#   ok    comment with the green run and close the issue when one is open;
#         does nothing when none is
#
# Needs GH_TOKEN, GH_REPO and RUN_URL in the environment. With
# COMPAT_WATCH_DRY_RUN=1 the lookup still runs but the writes are printed
# instead of made.
set -euo pipefail

mode="${1:?usage: compat_watch_issue.sh fail|ok}"
case "$mode" in fail | ok) ;; *) echo "unknown mode: $mode" >&2; exit 2 ;; esac
: "${RUN_URL:?RUN_URL is required}"

title="Compat watch: upstream Open WebUI or OpenClaw broke the integration"

gh_write() {
  if [ "${COMPAT_WATCH_DRY_RUN:-}" = "1" ]; then
    echo "DRY RUN: gh $*"
  else
    gh "$@"
  fi
}

# GitHub search matches substrings, so keep only the issue whose title is exactly ours.
existing=$(T="$title" gh issue list --state open --limit 100 --search "in:title \"$title\"" \
  --json number,title --jq 'map(select(.title == env.T))[0].number // empty')

case "$mode" in
  fail)
    body="The scheduled compat watch failed: $RUN_URL

It runs the end-to-end stack against \`open-webui:latest\` + \`openclaw@latest\` and checks the vendored Open WebUI handler against upstream. The pinned CI lane is unaffected until \`e2e/versions.env\` is bumped; the logs artifact on the run has the Gateway, Open WebUI and fake-model logs. The issue closes by itself on the next green scheduled run."
    if [ -n "$existing" ]; then
      gh_write issue comment "$existing" --body "Still failing: $RUN_URL"
    else
      gh_write issue create --title "$title" --body "$body"
    fi
    ;;
  ok)
    if [ -n "$existing" ]; then
      gh_write issue close "$existing" --reason completed \
        --comment "The scheduled compat watch is green again: $RUN_URL"
    else
      echo "No open compat-watch issue; nothing to close."
    fi
    ;;
esac
