# Versions

Which Open WebUI and OpenClaw versions the integration is tested on, the minimums, and what an older release loses, with the CI runs that show it.

Applies to: Pipe, Action, Filter

## Tested and minimum

| | Minimum | Tested in CI |
|---|---|---|
| Open WebUI | 0.12.4 | 0.12.5 and 0.13.0 |
| OpenClaw Gateway | 2026.9.3 | 2026.10.5 |
| Python inside Open WebUI (what Open WebUI ships) | 3.11 | 3.11 and 3.12 |
| Python for `install.py` | 3.10 | 3.10 to 3.13 |

The "Tested in CI" versions are the ones the end-to-end suite runs on for every pull request (`e2e/versions.env`). For Open WebUI that is two blocking lanes: the pinned release, and the next one an upgrade moves to. The OpenClaw minimum, and the Open WebUI releases below 0.12.0, come from the same suite run once on each older release; the results are below. The Open WebUI minimum, 0.12.4, does not: the suite passes on 0.12.0 to 0.12.3, and 0.12.4 is the first release without the two page bugs listed under What each feature needs.

Below the minimums the chat itself still works, but some features break (the table below). The worst cases: on Open WebUI 0.11.0 and older, tool cards keep spinning; on Open WebUI 0.11.1, and on OpenClaw 2026.9.1 or older, cron replies never appear; on Open WebUI 0.12.0 to 0.12.3, a tab that was loading the chat can show a reply cut, and an approval or question dialog can lose its answer.

## What each feature needs

| Feature | Needs | On an older release |
| --- | --- | --- |
| Tool cards (live, native) | Open WebUI 0.11.1 | 0.10.2 and 0.11.0: a card keeps spinning after its tool has finished, because 0.11.1 changed how Open WebUI pairs a call with its result and the Pipe follows 0.11.1 |
| Replies the agent starts (cron jobs, sub-agent results, restart recovery) | Open WebUI 0.12.0 | 0.11.1 and older: the reply is never written to the chat. The writer needs the chat row lock 0.12 added |
| Status dialog styling | Open WebUI 0.12.0 | Older releases lack a few of its CSS classes: slightly different spacing and text size, nothing missing |
| Status button in the composer on chat open | Open WebUI 0.12.0 | Appears after the next reply instead |
| A reply that finishes while a tab is loading the chat | Open WebUI 0.12.4 | 0.12.0 to 0.12.3: that tab can show the reply cut until it is reloaded. Those releases replace what reached the page during a load with what the load fetched; 0.12.4 keeps it ([open-webui#32761](https://github.com/open-webui/open-webui/issues/32761)) |
| An input dialog (an approval, a question) while the agent works | Open WebUI 0.12.4 | 0.12.0 to 0.12.3: a page update that reaches the tab while the dialog is open can take the dialog's answer, so Confirm reaches nobody ([open-webui#32742](https://github.com/open-webui/open-webui/issues/32742)) |
| Cron replies, and finishing a reply a Gateway restart cut off | OpenClaw 2026.9.3 | 2026.9.1 and older: a cron run in the chat's session is not recognised, so its reply is never shown; and in the restart test, a reply cut by a Gateway restart did not finish |
| A message sent while the agent works, answered in the same run | OpenClaw 2026.10.10 | Earlier Gateways: the message waits for the run to end and gets its own reply ([Reliability](./features/reliability.md#messages-sent-while-the-agent-works)). On `claude-cli` with 2026.9.3 the Gateway would drop it, so the Pipe holds it |
| Rate limits for a Gateway that only has a Claude CLI login | The `claude-cli-usage` Gateway plugin | OpenClaw 2026.9.1 and later report no Anthropic windows for that login; the dialog says so ([Status button](./install/status-action.md)) |

## Test runs

Each row is the full end-to-end suite on one pair, on GitHub's runners. The suite was not changed to suit older releases: a failure is the integration's behaviour on that release.

### Open WebUI, with OpenClaw 2026.9.3

| Open WebUI | Result | Failed tests | Run |
| --- | --- | --- | --- |
| 0.10.2 | 11 of 16 pass (1 skipped) | tool card stuck spinning; agent-started replies; sub-agent drawer (its parent reply never finished); Status CSS classes | [run](https://github.com/Eliav2/openclaw-openwebui-integration/actions/runs/38641673157) |
| 0.11.0 | 11 of 16 pass (1 skipped) | the same as 0.10.2 | [run](https://github.com/Eliav2/openclaw-openwebui-integration/actions/runs/38641677528) |
| 0.11.1 | 13 of 16 pass (1 skipped) | agent-started replies (two tests); Status CSS classes | [run](https://github.com/Eliav2/openclaw-openwebui-integration/actions/runs/38641680949) |
| 0.12.0 | pass | | [run](https://github.com/Eliav2/openclaw-openwebui-integration/actions/runs/39003313714) |
| 0.12.2 | pass | | [run](https://github.com/Eliav2/openclaw-openwebui-integration/actions/runs/39003315726) |
| 0.12.3 | pass | | [run](https://github.com/Eliav2/openclaw-openwebui-integration/actions/runs/39173684021) |
| 0.12.5 | pass (on OpenClaw 2026.10.5) | | [run](https://github.com/Eliav2/openclaw-openwebui-integration/actions/runs/41660465558) |
| 0.13.0 | pass (on OpenClaw 2026.10.5) | | [run](https://github.com/Eliav2/openclaw-openwebui-integration/actions/runs/41660471297) |

### OpenClaw, with Open WebUI 0.12.0

| OpenClaw | Result | Failed tests | Run |
| --- | --- | --- | --- |
| 2026.8.35 | 14 of 17 pass | cron replies (two tests); a guest's reply cut by a Gateway restart did not finish | [run](https://github.com/Eliav2/openclaw-openwebui-integration/actions/runs/38641688071) |
| 2026.9.1 | 14 of 17 pass | the same as 2026.8.35 | [run](https://github.com/Eliav2/openclaw-openwebui-integration/actions/runs/38641691738) |
| 2026.9.3 | pass | | [run](https://github.com/Eliav2/openclaw-openwebui-integration/actions/runs/39173684021) |
| 2026.10.3 | pass | | [run](https://github.com/Eliav2/openclaw-openwebui-integration/actions/runs/38641695395) |
| 2026.10.5 | pass | | every pull request |
| 2026.10.10 | pass (on Open WebUI 0.13.0) | | [run](https://github.com/Eliav2/openclaw-openwebui-integration/actions/runs/41675560751) |

## Keeping this page true

- A unit test fails when the "Tested in CI" row here or in the README differs from `e2e/versions.env`.
- To re-measure, run the end-to-end workflow by hand (**Actions > E2E > Run workflow**) with the `owui_image` or `openclaw_version` input set to the release, and update the tables. Do it when an upgrade touches one of the features above.
