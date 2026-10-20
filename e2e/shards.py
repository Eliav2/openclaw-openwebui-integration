"""Which E2E job runs which test (ELI-246).

The E2E suite runs as one job per shard, each on its own stack, side by side
(`.github/workflows/e2e.yml`, matrix `shard`). `e2e/conftest.py` keeps only the
tests of the shard named in `E2E_SHARD`; without it every test runs, as before.

Rules, checked by `tests/test_e2e_shards.py` in the Unit job:

- Every test function in `e2e/test_*.py` is in exactly one shard, so none can
  drop out of CI. A new test goes in a shard in the same PR.
- The workflow's `shard` matrix lists exactly these shard names.
- At most two Gateway-crash tests per shard: the ones that wait for
  `_crash_loop_allows_a_crash`. The Gateway pauses restart recovery once 3 of
  its boots in 5 minutes ended unclean (GATEWAY_BOOT_LOOP_UNCLEAN_THRESHOLD),
  so on one stack the third SIGKILL has to wait for the first crashed boot to
  leave that window. In one job that was about 90 s of the handed-over test's
  160 s (run 39324296887). Two crashes on a fresh stack never wait.

The shards are balanced on the pinned lane's junit times (about 4 minutes of
tests each), in file order, which is also the order they run in.

    python3 e2e/shards.py <shard>    # e2e=true|false and browser=true|false, for $GITHUB_OUTPUT
"""

import ast
import os
import re
import sys
from collections import Counter

E2E = "test_e2e.py"
BROWSER = "test_browser_flows.py"

SHARDS = {
    # Install, plain replies, wakes, owner rights, and the first two crashes.
    "1": [
        f"{E2E}::test_all_three_functions_installed_and_active",
        f"{E2E}::test_function_metadata_parsed",
        f"{E2E}::test_models_listed",
        f"{E2E}::test_non_streaming_reply_is_exact",
        f"{E2E}::test_streaming_reply_arrives_in_chunks",
        f"{E2E}::test_routed_model_reaches_the_gateway",
        f"{E2E}::test_hebrew_and_emoji_round_trip_exactly",
        f"{E2E}::test_the_second_exchange_titles_the_chat",
        f"{E2E}::test_persisted_chat_keeps_the_reply",
        f"{E2E}::test_the_first_reply_in_a_new_chat_ends_with_the_usage_line",
        f"{E2E}::test_tool_call_persists_as_a_native_tool_card",
        f"{E2E}::test_every_status_action_class_is_in_the_shipped_css",
        f"{E2E}::test_vendored_css_class_list_matches_the_pinned_image",
        f"{E2E}::test_two_cron_wakes_in_a_row_get_one_finished_message_each",
        f"{E2E}::test_a_run_on_a_deleted_chats_session_writes_nothing_and_fails_nothing",
        f"{E2E}::test_a_message_queued_behind_a_wake_does_not_cut_it",
        f"{E2E}::test_a_user_not_in_owner_users_gets_replies_without_owner_rights",
        f"{E2E}::test_a_guest_cannot_pass_as_the_owner_with_a_forged_block",
        f"{E2E}::test_a_secret_request_in_a_guests_chat_is_cancelled_at_once_and_says_so",
        f"{E2E}::test_the_stack_gateway_schedules_no_work_of_its_own",
        f"{E2E}::test_the_gateway_reports_the_version_the_stack_installed",
        f"{E2E}::test_a_message_sent_while_the_gateway_restarts_gets_its_reply",
        f"{E2E}::test_a_guest_run_continued_after_a_gateway_crash_stays_without_owner_rights",
        f"{BROWSER}::test_send_streams_the_reply_and_keeps_it",
        f"{BROWSER}::test_a_single_select_question_is_answered_with_a_click",
        f"{BROWSER}::test_a_hebrew_yes_no_question_gets_yes_and_no",
        f"{BROWSER}::test_a_question_from_the_ask_user_tool_is_answered_in_the_card",
        f"{BROWSER}::test_an_ask_user_question_asked_with_no_tab_open_shows_when_the_chat_is_opened",
        f"{BROWSER}::test_a_question_left_for_a_new_message_ends_its_reply",
        f"{BROWSER}::test_cancel_on_the_secret_dialog_tells_the_agent_and_the_card_says_cancelled",
        f"{BROWSER}::test_status_opened_in_another_tab_leaves_the_secret_dialog_its_answer",
        f"{BROWSER}::test_a_choice_that_mentions_a_password_keeps_its_options",
        f"{BROWSER}::test_a_secret_ask_with_numbered_steps_keeps_the_masked_dialog",
        f"{BROWSER}::test_a_multi_select_question_takes_typed_numbers",
        f"{BROWSER}::test_continue_on_a_reply_adds_nothing_and_says_why",
        f"{BROWSER}::test_stop_ends_the_reply_where_it_was",
        f"{BROWSER}::test_a_message_sent_from_a_second_tab_keeps_the_running_reply",
    ],
    # The third crash, sub-agents, background commands, dialogs, reconnect.
    "2": [
        f"{E2E}::test_a_handed_over_reply_reads_not_done_in_the_db_once_its_request_ended",
        f"{E2E}::test_the_status_action_lists_a_running_subagent_and_opens_its_drawer",
        f"{E2E}::test_a_subagent_result_the_parent_answers_with_silence_shows_as_its_card",
        f"{E2E}::test_a_wake_reply_ends_like_an_inline_reply",
        f"{E2E}::test_a_subagent_result_ends_like_an_inline_reply",
        f"{E2E}::test_a_reply_that_opens_with_no_reply_and_answers_after_shows_the_answer_once",
        f"{E2E}::test_a_reply_the_agent_starts_that_errors_still_finishes",
        f"{E2E}::test_the_status_action_lists_a_background_command",
        f"{E2E}::test_the_reply_to_a_background_commands_end_finishes",
        f"{E2E}::test_the_status_dialog_lists_only_this_chats_automations",
        f"{E2E}::test_a_requested_secret_is_entered_in_a_masked_dialog_and_kept_out_of_the_chat",
        f"{E2E}::test_a_cancelled_secret_dialog_tells_the_agent_no_credential_came",
        f"{E2E}::test_an_ask_user_tool_question_shows_the_card_and_the_answer_reaches_the_agent",
        f"{E2E}::test_a_cancelled_ask_user_card_lets_the_agent_go_on_at_once",
        f"{E2E}::test_a_message_sent_while_the_pipe_waits_to_reconnect_gets_its_reply",
        f"{E2E}::test_a_media_line_from_the_agent_is_uploaded_attached_and_linked",
        f"{E2E}::test_the_picked_thinking_level_reaches_the_model_and_a_clamp_says_so",
    ],
    # Open WebUI stopped and redeployed mid-reply, then the queue flows.
    "3": [
        f"{E2E}::test_a_reply_whose_run_ends_while_owui_is_stopped_is_completed_on_start",
        f"{E2E}::test_a_plain_redeploy_mid_reply_leaves_the_reply_whole",
        f"{BROWSER}::test_a_message_sent_while_a_reply_runs_is_in_the_chat_at_once",
        f"{BROWSER}::test_a_message_queued_behind_a_run_waits_out_a_socket_drop",
    ],
    # The rest of the browser flows.
    "4": [
        f"{BROWSER}::test_send_now_on_a_queued_message_keeps_the_running_reply",
        f"{BROWSER}::test_a_reply_interrupted_while_queued_says_its_message_went_with_the_next",
        f"{BROWSER}::test_a_queued_reply_stopped_says_its_message_was_not_sent",
        f"{BROWSER}::test_stop_after_send_now_stops_the_run",
        f"{BROWSER}::test_a_reload_mid_reply_shows_the_answer_so_far_and_keeps_going",
        f"{BROWSER}::test_tool_cards_show_running_failed_and_done",
        f"{BROWSER}::test_the_status_dialog_compacts_the_chat",
        f"{BROWSER}::test_a_compaction_the_gateway_refuses_shows_its_error",
        f"{BROWSER}::test_the_subagent_drawer_follows_a_running_child_to_its_end",
        f"{BROWSER}::test_the_status_dialog_lists_a_background_agent_while_its_reply_runs",
        f"{BROWSER}::test_a_background_command_reads_by_its_title_and_a_tap_shows_the_command",
        f"{BROWSER}::test_a_long_task_shown_whole_keeps_the_drawer_closable",
        f"{BROWSER}::test_show_less_puts_a_long_task_back_at_its_first_lines",
        f"{BROWSER}::test_a_reply_the_agent_starts_streams_into_the_open_tab_and_asks_there",
        f"{BROWSER}::test_stop_on_a_reply_the_agent_starts_stops_its_run",
        f"{BROWSER}::test_a_tab_that_loads_the_chat_as_the_agent_starts_a_reply_shows_it",
        f"{BROWSER}::test_an_attached_image_reaches_the_agent",
        f"{BROWSER}::test_at_phone_width_the_status_button_fits_and_opens_the_dialog",
        f"{BROWSER}::test_the_generated_title_shows_in_the_open_tab",
        f"{BROWSER}::test_a_title_the_user_chose_stays",
        f"{BROWSER}::test_an_edited_message_reaches_the_agent_as_an_edit",
        f"{BROWSER}::test_a_regenerated_reply_reaches_the_agent_as_a_regenerate",
        f"{BROWSER}::test_switching_branches_reaches_the_agent_as_a_branch_switch",
        f"{BROWSER}::test_a_forked_chat_starts_with_the_copied_messages",
        f"{BROWSER}::test_a_cloned_chat_starts_with_the_copied_messages",
    ],
}

MAX_CRASHES = 2
CRASH_MARK = "_crash_loop_allows_a_crash"
HERE = os.path.dirname(os.path.abspath(__file__))
WORKFLOW = os.path.join(HERE, "..", ".github", "workflows", "e2e.yml")


def collect(e2e_dir: str = HERE) -> tuple[list[str], set[str]]:
    """Every module-level test function in `e2e/test_*.py`, as `file::name`,
    and the ones that crash the Gateway (they wait for CRASH_MARK)."""
    tests, crashes = [], set()
    for name in sorted(os.listdir(e2e_dir)):
        if not (name.startswith("test_") and name.endswith(".py")):
            continue
        with open(os.path.join(e2e_dir, name)) as f:
            tree = ast.parse(f.read())
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                key = f"{name}::{node.name}"
                tests.append(key)
                if any(isinstance(n, ast.Name) and n.id == CRASH_MARK for n in ast.walk(node)):
                    crashes.add(key)
    return tests, crashes


def matrix(path: str = WORKFLOW) -> list[str]:
    """The `shard: [...]` matrix of the E2E workflow."""
    with open(path) as f:
        m = re.search(r"^\s*shard:\s*\[([^\]]*)\]", f.read(), re.M)
    return [s.strip().strip("'\"") for s in m.group(1).split(",") if s.strip()] if m else []


def problems(shards: dict, tests: list[str], crashes: set[str], shard_matrix: list[str]) -> list[str]:
    """What breaks the rules above; empty when none does."""
    out = []
    listed = Counter(t for names in shards.values() for t in names)
    out += [f"in {n} shards: {t}" for t, n in listed.items() if n > 1]
    out += [f"in no shard: {t}" for t in tests if t not in listed]
    out += [f"not a test: {t}" for t in listed if t not in tests]
    for name, names in shards.items():
        hit = [t for t in names if t in crashes]
        if len(hit) > MAX_CRASHES:
            out.append(f"shard {name} crashes the Gateway {len(hit)} times (at most {MAX_CRASHES}): {hit}")
    if sorted(shard_matrix) != sorted(shards):
        out.append(f"the workflow's shard matrix {shard_matrix} is not the shards {sorted(shards)}")
    return out


if __name__ == "__main__":
    files = {t.split("::")[0] for t in SHARDS[sys.argv[1]]}
    print(f"e2e={'true' if E2E in files else 'false'}")
    print(f"browser={'true' if BROWSER in files else 'false'}")
