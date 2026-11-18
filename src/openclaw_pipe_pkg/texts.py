# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built openclaw_pipe.py directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------
# What the Pipe itself shows a user (status lines, notes in a reply, its own
# card labels), in the user's Open WebUI language (ELI-512). One table: a new
# line goes here, never as a literal at its call site.
#
# The language is Open WebUI's `{{USER_LANGUAGE}}` variable, which its page
# sends with every chat request from the UI locale it keeps in the browser
# (`localStorage.locale`, e.g. `he-IL`; `en-US` when unset). A reply written
# with no request behind it (a turn writer after a restart, a cron wake) uses
# the language of that user's last request (`ui_lang_of`).
#
# Hebrew is for people who are not developers: plain words, no "gateway",
# "run" or "agent", and a status line short enough for one line on a 390 px
# phone (UI_STATUS_MAX_CHARS).

UI_LANG_DEFAULT = "en"
UI_LANG_HEBREW = "he"
UI_LANG_VAR = "{{USER_LANGUAGE}}"
# A status row is clamped to one line by Open WebUI: at 390 px about this many characters fit.
UI_STATUS_MAX_CHARS = 44

UI_TEXT = {
    # Status lines.
    "thinking": {"en": "Thinking...", "he": "חושב…"},
    "queued_behind": {"en": "Waiting -- your message is queued behind an active response...",
                      "he": "ממתין: ההודעה בתור…"},
    "queued_wait": {"en": "Queued: waiting for the current run to finish…",
                    "he": "בתור: מחכה שהמשימה הנוכחית תסתיים…"},
    "continuing": {"en": "Continuing the current task; your message is next…",
                   "he": "מסיים משימה קודמת, ההודעה שלך הבאה…"},
    "steer_pending": {"en": "Sent while the agent works: it reads it at its next step…",
                      "he": "נשלח תוך כדי עבודה, ייקרא בצעד הבא…"},
    "sending_message": {"en": "Sending your message…", "he": "שולח את ההודעה שלך…"},
    "sending_answer": {"en": "Sending answer...", "he": "שולח את התשובה שלך…"},
    "answer_sent": {"en": "Answer sent; continuing...", "he": "התשובה נשלחה, ממשיך…"},
    "stopped": {"en": "Stopped", "he": "נעצר"},
    "gateway_wait": {"en": "Waiting for the gateway…", "he": "אין חיבור כרגע, ממתין…"},
    "background_agents_one": {"en": "Waiting on 1 background agent", "he": "מחכה לעוזר שעובד ברקע"},
    "background_agents": {"en": "Waiting on {n} background agents", "he": "מחכה ל-{n} עוזרים שעובדים ברקע"},
    # Notes in a reply's text.
    "stopped_reason": {"en": "**Stopped.**", "he": "**נעצר.**"},
    "no_reply_text": {"en": "_(This turn produced no reply text.)_", "he": "_(אין תשובה בכתב הפעם.)_"},
    "sent_with_next": {"en": "_(This message was sent together with your next one: "
                             "the answer is in the next reply.)_",
                       "he": "_(ההודעה נשלחה יחד עם ההודעה הבאה שלך: התשובה בהודעה הבאה.)_"},
    "not_sent": {"en": "_(Stopped: this message was not sent to the agent.)_",
                 "he": "_(נעצר: ההודעה לא נשלחה.)_"},
    "send_failed": {"en": "_(This message was to go together with your next one, "
                          "which could not be sent: see the next reply.)_",
                    "he": "_(ההודעה הייתה אמורה להישלח יחד עם הבאה, שלא נשלחה: ראו בתשובה הבאה.)_"},
    # What a run that yielded without a word or an acknowledgment ends with.
    "yield_wait": {"en": "_(Waiting for the sub-agents: their answer comes in the next reply.)_",
                   "he": "_(מחכה לעוזרים שעובדים ברקע: התשובה שלהם תגיע בהודעה הבאה.)_"},
    "steered_into_running": {"en": "_(Sent to the reply that was already running: the agent answers it there.)_",
                             "he": "_(נוספה לתשובה שכבר הייתה בדרך: התשובה שם.)_"},
    "image_not_relayed": {"en": "_(Note: that image couldn't be relayed to the agent -- "
                                "only the text below was sent.)_",
                          "he": "_(שימו לב: לא הצלחתי לקבל את התמונה, נשלח רק הטקסט.)_"},
    "response_failed": {"en": "**The response failed.** Please try again.",
                        "he": "**התשובה נכשלה.** כדאי לנסות שוב."},
    "response_timed_out": {"en": "**The response timed out.** Please try again.",
                           "he": "**התשובה לקחה יותר מדי זמן.** כדאי לנסות שוב."},
    "pending_sent": {"en": "_(Sent at {when}, while the gateway was down. Delivered now.)_",
                     "he": "_(נשלחה ב-{when}, כשלא היה חיבור. נמסרה עכשיו.)_"},
    "gateway_lost_run": {"en": "_(The Gateway restarted while this reply was running and did not continue it.)_",
                         "he": "_(המערכת הופעלה מחדש באמצע התשובה, והתשובה לא הושלמה. אפשר לשלוח שוב.)_"},
    "turn_cut_off": {"en": "_(This reply was cut off: the run was interrupted again after "
                           "{tries} attempts to continue it.)_",
                     "he": "_(התשובה נקטעה ולא הצלחתי להשלים אותה. אפשר לשלוח שוב.)_"},
    "orphan_stub": {"en": "*This reply was cut off before it finished (Open WebUI restarted or "
                          "lost the run), and no reply for it could be recovered from OpenClaw. "
                          "Send the message again if you still need an answer.*",
                    "he": "*התשובה נקטעה לפני שהסתיימה ולא הצלחתי לשחזר אותה. "
                          "אפשר לשלוח את ההודעה שוב.*"},
    "orphan_stub_silent": {"en": "*The run for this message stopped without leaving a reply in the transcript.*",
                           "he": "*הטיפול בהודעה הזו נעצר בלי תשובה.*"},
    "compacting": {"en": "🧹 _Compacting context…_", "he": "🧹 _מסכם את השיחה כדי לפנות מקום…_"},
    "compaction_failed": {"en": "🧹 _Context compaction did not complete: the conversation goes on uncompacted._",
                          "he": "🧹 _הסיכום לא הושלם, השיחה ממשיכה כרגיל._"},
    "compaction_skipped": {"en": "🧹 _Context compaction skipped: nothing to compact yet._",
                           "he": "🧹 _אין עדיין מה לסכם._"},
    "compaction_stopped": {"en": "🧹 _Context compaction stopped._", "he": "🧹 _הסיכום נעצר._"},
    "compacted_summary": {"en": "🧹 Context compacted", "he": "🧹 השיחה סוכמה"},
    "compacted_was": {"en": " · was ~{tokens} tokens", "he": ""},
    "compacted_body": {"en": "Older messages in this conversation were summarized to free up the model's "
                             "context. The agent continues from that summary plus the most recent messages; "
                             "everything stays visible here.",
                       "he": "הודעות ישנות בשיחה סוכמו כדי לפנות מקום. ההמשך מתבסס על הסיכום ועל "
                             "ההודעות האחרונות, והכול עדיין מופיע כאן."},
    # The Pipe's own card label.
    "subagent_card": {"en": "Sub-agent", "he": "עוזר"},
    # A tool card's header (Open WebUI's `display_labels`): running, then done. English keeps the
    # tool's own name, plus the title the agent gave the call; Hebrew says what it does.
    "tool_command_running": {"en": "{name}{title}", "he": "מריץ פקודה"},
    "tool_command": {"en": "{name}{title}", "he": "הריץ פקודה"},
    "tool_read_running": {"en": "{name}{title}", "he": "קורא"},
    "tool_read": {"en": "{name}{title}", "he": "קרא"},
    "tool_edit_running": {"en": "{name}{title}", "he": "כותב לקובץ"},
    "tool_edit": {"en": "{name}{title}", "he": "כתב לקובץ"},
    "tool_search_running": {"en": "{name}{title}", "he": "מחפש ברשת"},
    "tool_search": {"en": "{name}{title}", "he": "חיפש ברשת"},
    "tool_fetch_running": {"en": "{name}{title}", "he": "פותח דף"},
    "tool_fetch": {"en": "{name}{title}", "he": "פתח דף"},
    "tool_helper_running": {"en": "{name}{title}", "he": "מפעיל עוזר"},
    "tool_helper": {"en": "{name}{title}", "he": "הפעיל עוזר"},
    "tool_lookup_running": {"en": "{name}{title}", "he": "מחפש"},
    "tool_lookup": {"en": "{name}{title}", "he": "חיפש"},
    "tool_tools_running": {"en": "{name}{title}", "he": "מחפש כלי"},
    "tool_tools": {"en": "{name}{title}", "he": "מצא כלי"},
    "tool_tool_running": {"en": "{name}{title}", "he": "משתמש בכלי"},
    "tool_tool": {"en": "{name}{title}", "he": "השתמש בכלי"},
}

# The last UI language each Open WebUI user sent: for replies written without a request.
_UI_LANG_BY_USER: dict[str, str] = {}


def ui_lang(metadata) -> str:
    """`he` or `en`: the UI language of a request's metadata (`{{USER_LANGUAGE}}`)."""
    variables = (metadata or {}).get("variables") if isinstance(metadata, dict) else None
    lang = (variables or {}).get(UI_LANG_VAR) if isinstance(variables, dict) else None
    code = str(lang or "").split("-", 1)[0].lower()
    return code if code in (UI_LANG_DEFAULT, UI_LANG_HEBREW) else UI_LANG_DEFAULT


def ui_lang_remember(user_id, metadata) -> str:
    """The request's language, kept as this user's for replies written later without one."""
    lang = ui_lang(metadata)
    if user_id:
        _UI_LANG_BY_USER[user_id] = lang
    return lang


def ui_lang_of(user_id) -> str:
    """The language of `user_id`'s last request since this process started, else English."""
    return _UI_LANG_BY_USER.get(user_id or "", UI_LANG_DEFAULT)


def ui_text(key: str, lang: str = UI_LANG_DEFAULT, **values) -> str:
    """Line `key` of UI_TEXT in `lang` (English when the table has no `lang` for it), formatted
    with `values`. An empty line is a line: a part with nothing to say in that language."""
    entry = UI_TEXT[key]
    text = entry[lang] if lang in entry else entry[UI_LANG_DEFAULT]
    return text.format(**values) if values else text


def ui_texts(key: str, **values) -> tuple[str, ...]:
    """Line `key` in every language: to recognise it in a saved reply whatever its language."""
    return tuple(text.format(**values) if values else text for text in UI_TEXT[key].values())
