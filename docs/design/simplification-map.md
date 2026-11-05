# Simplification map

Phase 1 of the size cut: where the code is, what it does and where it can shrink. This is for review before any cut. Behaviour stays the same and nothing new is added. Measured on `main` a35c074 (2026-11-05).

## Size today

| Area | Lines | KB | Note |
|---|---:|---:|---|
| `src/openclaw_pipe_pkg/` (21 files) | 21,637 | 1,028 | 14,336 code, 2,433 comment, 2,937 docstring, 2,213 blank |
| `tests/` | 31,970 | 1,612 | `test_pipe_unit.py` alone: 17,530 lines, 985 tests, 153 classes |
| `e2e/` | 7,764 | 405 | |
| `scripts/` (25 files) | 5,135 | 249 | |
| `build.py`, `install*.py` | 1,554 | 63 | |
| Built bundles (generated) | 28,816 | 1,390 | `openclaw_pipe.py` 17,768 lines, `openclaw_status_action.py` 10,656, filter 392 |

**A third of `src/` by bytes is prose:** docstrings are 184 KB (18%) and comments 152 KB (15%). In `tests/`, docstrings are 201 KB (13%).

Embedded front-end code in `src/`: about 2,000 lines are string literals of 15 lines or more. The biggest are the Status dialog page (`action.py:108`, 1,042 lines) and the composer button snippet (`composer.py:78`, 496 lines).

## What each module does

| Module | Lines | What it does |
|---|---:|---|
| `gateway.py` | 4,780 | The WebSocket client to the OpenClaw Gateway (connect, hello, device identity, reconnect). Routes run events to live replies, steering, parked cancels, session-row waiters, transcript (`session.message`) subscriptions, and `_TurnRenderer` (Gateway events to Open WebUI output items). Shared with the Action. |
| `writer.py` | 3,576 | The turn writer: a reply no live request streams (cron wakes, announces, restart recovery, a reply whose request Open WebUI cancelled). It claims or inserts the message, streams it to tabs, ends it from the transcript when the session settles, and reconciles replies left unfinished after an Open WebUI restart. |
| `action.py` | 3,736 | The Status Action: the Status dialog (usage, rate limits, sub-agents, background tasks, compaction, per-chat controls). Its HTML and JS page is 1,042 lines. |
| `pipe.py` | 2,909 | The `Pipe` class: model list, valves, and `_PipeTurn` (prepare, admit, `wait_and_send`, consume, finish), the life of one chat request. Also title generation and the cancel/Send now handling. |
| `askuser.py` | 1,388 | Questions to the user: the native ask_user card, the `needs input:` marker, secret requests and approvals answered from the chat. |
| `media.py` | 620 | `MEDIA:` directives, images in and out, and the small file server. |
| `composer.py` + `composer_watch.py` | 948 | The Status button in the composer (the JS snippet) and the watcher that re-injects it into tabs. |
| `channels.py` | 603 | Open WebUI channel threads as Gateway sessions. |
| `slash_prompts.py` | 560 | Syncs OpenClaw skills/commands into Open WebUI prompts. |
| `turns.py` | 483 | Pure rules for turn ownership (DESIGN 2.x): who continues a cut run, resume or finalize, transcript spans. Also the shielded Open WebUI writes. |
| `emit.py` | 480 | Status lines, usage formatting, the `execute` sender and the dialog guard. |
| `history.py` | 467 | Rebuilds a reply from `chat.history` (tool cards and text). |
| `approvals.py`, `models.py`, `state.py`, `thinking*.py`, `identity.py`, `_prelude.py` | 1,082 | Exec approvals, model sync and patch errors, the persisted state file, thinking levels plus the Filter, device identity, imports. |

## Where the size comes from

### 1. Prose: comments and docstrings (biggest lever, no behaviour risk)

There are 5,370 comment and docstring lines in `src/` (336 KB). Many tell history rather than the code's contract: ELI numbers, probe dates, CI run ids, which PR found what, quotes from Gateway dist files with line numbers. That history already lives in the PRs, in `docs/design/`, and in git. One example: `_parse_gateway_url` has a 22-line docstring on top of 90 lines of code that `urllib.parse` could do in about 25.

**Proof per PR:** the module's AST with docstrings stripped is identical before and after (a script compares `ast.dump`). That proves no code changed. The unit and e2e suites are then a formality.

### 2. Workarounds for upstream bugs that are fixed now

I checked each one's live state (2026-11-05), and for Open WebUI which release has the fix.

| Ours | Upstream | State | Removable when | Est. lines (src / tests) |
|---|---|---|---|---:|
| ELI-230 tab probe and repair: `_tab_probe_js`, `_probe_tabs`, `_probe_tab`, `_repair_tab`, the end re-send (`writer.py`) | [open-webui#32761](https://github.com/open-webui/open-webui/issues/32761) | Fixed in **0.12.4**: `loadChat` keeps live updates received while loading (checked in Chat.svelte of v0.12.4, v0.12.5 and v0.13.0) | Minimum Open WebUI ≥ 0.12.4 | ~120 / ~250 |
| ELI-195 hold of every `execute` to a tab with an open input dialog (`emit.py` `_dialog_open_on` plus 5 call sites) | [open-webui#32742](https://github.com/open-webui/open-webui/issues/32742) | Fixed in **0.12.4** (PR #32744, merge commit is in v0.12.4) | Minimum ≥ 0.12.4 | ~60 / ~150 |
| `_save_response_stream` signature probe (`inspect.signature`) | 0.12.3 API change | n/a | Minimum ≥ 0.12.3 | ~10 / ~20 |
| ELI-205 shielded writes (`turns.py` `_uncancellable`) | [open-webui#32741](https://github.com/open-webui/open-webui/issues/32741) | Its PR #32745 was not merged, but [open-webui#32617](https://github.com/open-webui/open-webui/pull/32617) in **0.12.4** keeps the SQLite connection when a cancelled task's error is a `CancelledError`. The e2e `raw` probe (no shield) passes on 0.12.5 and fails on 0.12.3 (sandbox, 2026-11-05) | Keep: the shield is also a contract. A Stop during a save lets the write finish (the reply's end, a claim, the title), and the unit tests assert that. Without it the write rolls back. Dropping it would change behaviour, not just remove a workaround | ~40 |
| ELI-215: end an agent-started run on its own final | [openclaw#177065](https://github.com/openclaw/openclaw/issues/177065) | Closed 2026-10-21. But on 2026.10.10 a follow-up-queued run still ends with no idle push (sandbox, PR #300) | Keep; the gap still exists in another shape | 0 |
| ELI-194 `tasks.cancel` fallback for wakes | [openclaw#177061](https://github.com/openclaw/openclaw/issues/177061) | Open | Keep | 0 |
| Status button's own POST instead of the action save | [open-webui#32658](https://github.com/open-webui/open-webui/issues/32658) | Closed as not a bug (PR #32660 unmerged) | Keep | 0 |
| Stop vs Send now detected by `aria-label` (ELI-87) | [open-webui#32336](https://github.com/open-webui/open-webui/issues/32336) | Open | Keep | 0 |

### 3. Compat paths for versions we don't run

The documented minimums ([Versions](../versions.md)) are **Open WebUI 0.12.0** and **OpenClaw 2026.9.3**. Live runs Open WebUI 0.12.5 and OpenClaw 2026.10.10; CI tests 0.12.5/0.13.0 and 2026.10.5/2026.10.10-beta.1. Every path below is inside the supported range today, so cutting it changes that range: **Eliav's call (phase 3 rule)**.

| Path | Gate | Est. lines (src / tests) |
|---|---|---:|
| 2026.9.3: a cut run's row kept `running` (`judges_cut_runs_after_listening` and its callers), the claude-cli message hold on 9.3, `tasks.cancel` on 9.3 | `turns.py:296`, `writer.py:2413`, `pipe.py` claude-cli branches | ~150 / ~400 |
| Before 2026.10.10, no mid-run steering: a message sent while a run works waits for it in `wait_and_send`, park/resend of queued turns (ELI-56/ELI-88) | `steers_mid_run_messages` (10 call sites) | ~300–600 / ~1,000. The hold path stays for images, model changes and wakes even on 10.10, so only the branches that steering makes unreachable go. Needs a per-branch audit |
| Open WebUI 0.12.0–0.12.3 | the rows in section 2 | see section 2 |

**Proposal for Eliav:** raise the minimums to **Open WebUI 0.12.4** and **OpenClaw 2026.10.10** (both run live; CI already covers 10.10). Users on an older Gateway keep the last release that supported it (tag it before the cut).

### 4. Duplicated logic and over-sized functions

- **`_PipeTurn`** (`pipe.py`): `consume` 300, `prepare` 295, `finish` 222, `wait_and_send` 152, `admit` 126 lines. Several waits repeat the same loop: read `chat.history`, check the row, wait for a session change (`pipe.py:1250`, `writer.py:1330`, `_await_recovery_turn`). One helper fits them all.
- **`_GatewayConnection.__init__`**: 233 lines of state with history comments. The routing tables (consumers, turn pumps, run targets, renderers, delivered-live, skip runs) can be grouped by owner.
- **`Action._run_status`**: 429 lines in one method.
- **Row reads**: `sessions.list {search}` then "find my row" appears at least 4 times (`_turn_reread_row`, `_adopt_turn_message`, `_session_list_active`, the pipe's `_session_run_active`). One helper.
- **Smaller ones**: `_parse_gateway_url` (113 lines to about 30); `_explain_session_patch_failure` (61).

Estimate: −800 to −1,500 src lines. Medium risk, so these go last, one function per PR.

### 5. Dead code

`vulture` (60% confidence) finds almost nothing. The fragments are one namespace and most hits are Open WebUI entry points. The only real candidate is `channels._parse_channel_session_key`. Real dead code here is code behind conditions that can no longer happen (sections 2 and 3), not unreferenced functions. Estimate: −50 lines.

### 6. Scripts nobody calls

| Script | Lines | Callers |
|---|---:|---|
| `scripts/owui_internals_diff.py` | 191 | none |
| `scripts/probe_cancelled_owui_write.py` | 44 | none (the #32741 repro) |
| `scripts/social_preview.py` | 87 | `docs/development.md` only |
| `scripts/demos/record_live.py` | 319 | `docs/development.md` only |
| `scripts/migrate_stale_chat_models.py` | 268 | docs + its own test (a one-time migration) |

The first two can go (−235). The last three are used by hand: Eliav decides (up to −674 more plus a test).

### 7. Tests

- **Prose:** 3,162 docstring and 743 comment lines (252 KB). Most test docstrings retell the incident. One line saying what the test proves is enough; the incident is in the PR. Proved the same way as section 1 (the AST outside docstrings is unchanged).
- **Tests of removed code** go with sections 2 and 3 (about −1,800 together).
- **Duplicates:** the big classes (`NoDroppedMessagesTests` 1,671 lines/55 tests, `TurnWriterEndTests` 882/49, `SessionIdRunTests` 755/32, `MidRunSteeringTests` 542/21) repeat the same 20–40-line setup. Shared fixtures plus parametrizing cases that differ only in data: −2,000 to −4,000 lines. Each PR names every test merged and shows the asserts are a superset.

### 8. Do the bundles need to be committed?

Yes, for now. Stock users install in two ways that read the committed files:
- pasting `openclaw_pipe.py` into Admin → Functions ([manual install](../install/manual.md));
- the no-clone installer, which downloads `openclaw_pipe.py` from `main` over raw.githubusercontent.com, a command already in users' hands ([installer](../install/installer.md)).

`.gitattributes` already marks them `linguist-generated`, so PR diffs collapse them. Moving them to release assets would need a release workflow and a new installer URL, and would break the documented command for anyone with the old one. It removes no complexity: they are generated from `src/` and shrink as `src/` does.

## Target

| Area | Today | Target | Cuts it comes from |
|---|---:|---:|---|
| `src/` | 21.6k lines / 1,028 KB | **−35% lines / −45% KB** at the current supported range; **−40% / −50%** with the minimums raised | prose −2,700; upstream fixes −190; compat −450 to −750; restructure −800 to −1,500; dead −50 |
| `tests/` | 32.0k / 1,612 KB | −30% | prose −2,000; removed code −1,800; duplicates −2,000 to −4,000 |
| `scripts/` | 5.1k | −5% to −18% | section 6 |
| Bundles | 28.8k | follow `src/` | |

## Proposed cuts, ranked

Order (decided): the AST check with cut 1, then 5, 6 and 9, with the other prose cuts interleaved between code cuts. 7 and 8 wait for ELI-356.

| # | Cut | Est. lines | Risk | Needs |
|---:|---|---:|---|---|
| 1 | Prose diet in `gateway.py` and `writer.py` (AST-identical) | −1,500 | none | - |
| 2 | Prose diet in `pipe.py`, `action.py`, `askuser.py` | −800 | none | - |
| 3 | Prose diet in the other src modules | −400 | none | - |
| 4 | Prose diet in tests, a file at a time (AST-identical outside docstrings) | −2,000 | none | - |
| 5 | Delete `owui_internals_diff.py`, `probe_cancelled_owui_write.py`, `migrate_stale_chat_models.py` (+ test, docs), `_parse_channel_session_key` | −560 | low | decided |
| 6 | Drop the ELI-230 tab probe and the ELI-195 dialog hold (fixed in Open WebUI 0.12.4) | −180 src / −400 tests | low | decided: minimum Open WebUI 0.12.4 |
| 7 | Drop the 2026.9.3 paths | −150 / −400 | medium | waits for ELI-356 (minimum OpenClaw 2026.10.10, ~2026-11-22..26) |
| 8 | Drop the branches steering makes unreachable on 2026.10.10 | −300 to −600 / −1,000 | medium | waits for ELI-356 |
| 9 | One "wait on transcript/row/session change" helper; one row-read helper | −200 to −300 | medium | - |
| 10 | Split `_PipeTurn` and `Action._run_status` into smaller steps, dropping repeated branches | −500 to −1,000 | medium-high | - |
| 11 | Merge duplicate unit tests (shared fixtures, parametrize) | −2,000 to −4,000 | low (no assert lost) | - |
| 12 | Scripts used by hand only | - | - | closed: `social_preview.py` and `record_live.py` stay |
| 13 | Drop the ELI-205 shield (`_uncancellable`) | - | - | closed: the wedge is fixed in 0.12.4, but the shield also makes a write that a Stop interrupts finish instead of rolling back, so removing it changes behaviour |

Each PR reports src/tests/scripts lines before and after, runs the unit suite and the e2e shards, then goes to the manager's live gate. It lists any test deleted (code gone) or merged (true duplicate). It rebases the magic baseline with `--update` when baselined lines go away. It doesn't touch `push/` or the web-push function (W1).

## Decisions (2026-11-05)

1. **Open WebUI minimum: 0.12.4, now (Eliav).** Cut 6 goes ahead. The PR also records the new minimum in [Versions](../versions.md) and the release notes.
2. **OpenClaw minimum: unchanged for now (Eliav).** The pre-10.10 and pre-steering paths stay, so cuts 7 and 8 wait. The minimum moves to 2026.10.10 around 2026-11-22..26 ([ELI-356](https://linear.app/eliavb/issue/ELI-356)).
3. **Scripts (Eliav):** `migrate_stale_chat_models.py` goes, with its test and its docs mention (in cut 5). `social_preview.py` and `record_live.py` stay. Cut 12 is closed.
4. **Gate for prose-only PRs (manager):** they skip the live gate when a CI check proves it. The check compares the AST with docstrings and comments stripped, for `src/` and the built bundles, between base and head, and fails on any difference. A PR labelled `prose-only` must pass it; a PR that does not pass it gets the live gate as usual.
5. **Prose rule (manager):** keep a comment that explains a non-obvious why (an upstream bug with its link, an ordering constraint). Drop retold incident stories and comments that restate the code. Keep every upstream issue link, since cuts 7 and 8 need them.
6. **Order and reporting (manager):** real code cuts go in early, interleaved with the prose PRs: the AST check with cut 1, then 5, then 6, then 9. Cut 13 (the ELI-205 shield) was examined after cut 6 and closed: the shield is also a finish-on-cancel contract. Every report gives `src/` as code lines (without comments and docstrings) and as total lines, before and after.
