---
name: "openclaw-owui-ask-user"
description: "Ask the user a question that pops a real dialog in Open WebUI, using the OpenClaw needs input: marker."
---

# Asking the user a question in Open WebUI

When you are talking to a user through the OpenClaw ⇄ Open WebUI pipe, you can
stop mid-run and ask them something in a real dialog instead of guessing.

**Do not use a provider-native ask tool** such as Claude Code's
`AskUserQuestion`. Those do not reach the user on this surface. The pipe watches
your assistant text for a marker instead.

## How to ask

Emit a line that **starts** with `OpenClaw needs input:` and nothing before it on
that line. Put the question on the next line:

```
OpenClaw needs input:
Which database should I use?
```

The pipe intercepts the whole block, renders it as a dialog, and feeds the
user's answer straight back to you in the same stream. Keep going once you have
it. The raw marker is never shown to the user.

## Offering choices

Number the options and the dialog becomes clickable buttons:

```
OpenClaw needs input:
Which database should I use?
1. Postgres
2. SQLite
3. Let me decide later
```

You get back the label the user clicked, for example `SQLite`.

For "pick any number of these", include a phrase like `select all that apply`
or `(multiselect)` in the question. The dialog then renders checkboxes and a
Submit button, and you get a comma-joined string back.

## Passwords and secrets

If the question mentions a secret, a password, an API key, or a token, the
dialog switches to a masked input automatically. You do not need to do anything.

## Rules that matter

- **The marker must begin a line.** `... done.OpenClaw needs input:` glued onto
  the end of a sentence will not be picked up cleanly.
- **Emit the block exactly once.** Repeating it produces a confusing dialog.
- **Ask one question at a time.** The dialog shows one prompt.
- **Keep option labels short.** They become buttons.
- Do not paste the marker into ordinary prose. Mentioning it while explaining
  something is fine, but it must not start a line unless you mean it.

## When there is no answer

If the user closes the dialog or does not respond, you get nothing back. Say what
you are assuming and carry on, or stop and explain what you need. Do not loop.
