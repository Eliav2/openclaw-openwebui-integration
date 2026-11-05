# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built openclaw_pipe.py directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------


# ---------------------------------------------------------------------------
# What the user sees when a run or a send fails (S13)
#
# The Gateway's error string is a developer's line ("prepared reply dispatch
# runtime owner was not published for main"), the same for every user. The user
# gets one plain sentence in the language of their own message, and the raw
# text stays one click away in a collapsed block for whoever debugs it.
# ---------------------------------------------------------------------------

ERROR_SEND = "send"              # chat.send was refused or failed
ERROR_RUN = "run"                # the run ended in an error
ERROR_TIMEOUT = "timeout"        # the Gateway did not answer in time
ERROR_UNFINISHED = "unfinished"  # the run ended without a reply or a reason
ERROR_DETAILS = "details"        # the summary of the collapsed raw text

LANG_EN = "en"
LANG_HE = "he"

# The Hebrew block of Unicode: its letters and points.
_HEBREW_FIRST, _HEBREW_LAST = "֐", "׿"
_URL_RE = re.compile(r"https?://\S+")
_BACKTICKS_RE = re.compile(r"`+")
# A code fence is at least this many backticks.
_MIN_FENCE = 3

_ERROR_TEXT = {
    LANG_EN: {
        ERROR_SEND: "**Your message couldn't be sent.** Please try sending it again.",
        ERROR_RUN: "**Something went wrong while answering.** Please try again.",
        ERROR_TIMEOUT: ("**The assistant took too long to respond.** This is usually "
                        "temporary: please send your message again."),
        ERROR_UNFINISHED: "**The reply didn't finish.** Please send your message again.",
        ERROR_DETAILS: "Technical details",
    },
    LANG_HE: {
        ERROR_SEND: "**לא הצלחתי לשלוח את ההודעה.** אפשר לנסות לשלוח אותה שוב.",
        ERROR_RUN: "**משהו השתבש בזמן שעניתי.** אפשר לנסות שוב.",
        ERROR_TIMEOUT: ("**לא קיבלתי תשובה בזמן.** זה בדרך כלל חולף, "
                        "אפשר לשלוח את ההודעה שוב."),
        ERROR_UNFINISHED: "**התשובה לא הסתיימה.** אפשר לשלוח את ההודעה שוב.",
        ERROR_DETAILS: "פרטים טכניים",
    },
}


def _message_language(text) -> str:
    """`LANG_HE` when more words of `text` have Hebrew letters than Latin ones
    (links left out), else `LANG_EN`: the script of the message picks the
    language, no setting. Words, not letters, so one long foreign name in a
    Hebrew sentence does not outweigh it."""
    hebrew = latin = 0
    for word in _URL_RE.sub("", str(text or "")).split():
        if any(_HEBREW_FIRST <= c <= _HEBREW_LAST for c in word):
            hebrew += 1
        elif any(c.isascii() and c.isalpha() for c in word):
            latin += 1
    return LANG_HE if hebrew > latin else LANG_EN


def _fenced(raw: str) -> str:
    """`raw` as a code block that no backticks inside it can close."""
    longest = max((len(m) for m in _BACKTICKS_RE.findall(raw)), default=0)
    fence = "`" * max(_MIN_FENCE, longest + 1)
    return f"{fence}\n{raw}\n{fence}"


def _plain_error(kind: str, raw, user_text="") -> str:
    """The sentence for an error of `kind`, in the language of `user_text`,
    followed by `raw` (the Gateway's own text) in a collapsed block."""
    text = _ERROR_TEXT[_message_language(user_text)]
    raw = str(raw or "").strip()
    if not raw:
        return text[kind]
    return (f"{text[kind]}\n\n<details>\n<summary>{text[ERROR_DETAILS]}</summary>\n\n"
            f"{_fenced(raw)}\n\n</details>")
