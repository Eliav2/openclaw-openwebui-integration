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
that line. Put a short header on the next line, then the question:

```
OpenClaw needs input:
Database
Which database should I use?
```

The pipe intercepts the whole block, renders it as Open WebUI's inline question
card, and feeds the user's answer straight back to you in the same stream. Keep
going once you have it. The raw marker is never shown to the user. The user can
always type their own answer in the card's Other field.

## Offering choices

Number the options, **one per line**, and each becomes a clickable row. Add
` - ` and a short description after a label to show it beside the choice.
Options on one line (`1. Red 2. Blue`) are not read as options: the user gets
only a typed answer.

```
OpenClaw needs input:
Database
Which database should I use?
1. Postgres - robust, needs a server
2. SQLite - one file, zero setup
3. Let me decide later
```

You get back the label the user clicked, for example `SQLite`, or the text they
typed instead. **The card tags the first option Recommended, so list your
recommendation first.** A yes/no question (`(y/n)`) shows Yes and No and returns
`yes` or `no`.

## Several questions at once

To ask up to three related questions in one card, put a JSON object after the
marker line:

```
OpenClaw needs input:
{"questions": [
  {"header": "Frequency", "question": "How often should backups run?",
   "options": [{"label": "Daily", "description": "Every night at 03:00"},
               {"label": "Weekly", "description": "Sundays"}]},
  {"header": "Storage", "question": "Where should they go?",
   "options": ["NAS - local, fast", "Cloud - offsite"]}
]}
```

The user steps through the questions and submits once. You get one
`Header: answer` line per question. Add `"allow_other": false` to a question to
hide the typed answer; a question with no `options` is free text.

For "pick any number of these", include a phrase like `select all that apply`
or `(multiselect)` in the question, with the options numbered one per line. The
card then lists the options by number and the user types the ones they pick;
you get the picked options back comma-joined.

## Passwords and secrets

If the question mentions a secret, a password, an API key, or a token, the
dialog switches to a masked input automatically. You do not need to do anything.

## Rules that matter

- **The marker must begin a line.** `... done.OpenClaw needs input:` glued onto
  the end of a sentence will not be picked up cleanly.
- **Emit the block exactly once.** Repeating it produces a confusing dialog.
- **One block per turn.** Need several answers? Use the JSON form, at most three
  questions.
- **Keep option labels short.** Put the detail in the description.
- Do not paste the marker into ordinary prose. Mentioning it while explaining
  something is fine, but it must not start a line unless you mean it.

## When there is no answer

If the user presses Cancel or never responds, you get nothing back. Say what
you are assuming and carry on, or stop and explain what you need. Do not loop.
