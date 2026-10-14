# Docs

Every page of the OpenClaw for Open WebUI docs, with one line on what it covers. Each page opens with a one-line summary, then an `Applies to:` line (Pipe, Action, Filter) and the valves it uses, so a person or an agent can tell from the top whether it is the right page.

The three functions: the **Pipe** is the chat integration, the **Status Action** adds the Status button, and the **thinking filter** adds the per-chat thinking control.

## Install

- [Quick start](./install/quickstart.md): the shortest path from nothing to the first reply, with a check at each step.
- [The installer](./install/installer.md): `install.py`, its prerequisites, the wizard, environment variables and flags, the no-clone form, what `install` does, and its commands.
- [Manual install](./install/manual.md): paste the functions in the admin panel and set the valves yourself, with no admin password given to a script.
- [Device pairing](./install/pairing.md): approving the device on the Gateway, how the identity persists, `STATE_DIR` and its `/tmp` fallback.
- [Status button](./install/status-action.md): installing the Status Action, the composer Status button and the rate-limit plugin.
- [Thinking control](./install/thinking-filter.md): installing the thinking filter and attaching it to models.
- [Agent skills](./install/agent-skills.md): teaching your agent the two conventions (questions and media), installing and updating the skills.
- [Update and uninstall](./install/update-uninstall.md): updating, `backups/`, rolling back, uninstalling, revoking the device.
- [Two hosts](./install/two-hosts.md): Open WebUI and OpenClaw on different machines: what runs where, the network, approval.

## Use

- [Why this exists](./why.md): the native protocol versus a text API, and the full comparison with OpenClaw's `/v1` endpoint.
- [Features](./features/index.md): the full feature list, each line linking to its page.
  - [Chat](./features/chat.md): streaming, tool cards, tool failures, image input, media, the status line, auto-title.
  - [Questions](./features/questions.md): question cards and dialogs, which kind you get, and what happens on a reconnect.
  - [Status](./features/status.md): the Status dialog, Compact, the sub-agent list and drawer, the composer button.
  - [Sessions and models](./features/sessions-and-models.md): one session per chat, the model picker, its size cap, `DEFAULT_MODEL`.
  - [Replies from the agent](./features/agent-turns.md): replies with no message from you: cron jobs, heartbeats, `sessions_send`, sub-agent results.
  - [Reliability](./features/reliability.md): the connection, queueing, reload and restart safety, recovery, Stop and Send now, redeploys, background tasks, errors.
  - [Owner users](./features/owner-users.md): `OWNER_USERS`, for an Open WebUI that people other than you sign in to.
- [Configuration](./configuration.md): every valve of the three functions, generated from the code.
- [Metadata contract](./metadata-contract.md): the Sender and Conversation info blocks every message carries, their stability guarantees, and how an agent should read them.

## Fix

- [Troubleshooting](./troubleshooting.md): a symptom table, the errors you can see in the chat, where the logs are, and what to put in a bug report.
- [Versions](./versions.md): tested versions, minimums, and what an older release loses.
- [Security](./security.md): the secrets, the media file server, the agent's privileges, owner users, the scripts in your browser.

## Contribute

- [Architecture](./architecture.md): how a message travels from Open WebUI to the agent and back, why the Pipe is an async generator, and the repo's file layout.
- [Development](./development.md): editing the `src/` fragments, the build and its drift guard, running the tests, driving the integration over the Open WebUI API.
- [Rehydration and persistence](./rehydration-persistence.md): how Open WebUI persists an assistant message and why the Pipe saves snapshots mid-run. Read it before touching snapshots or streaming.

## Design

- [Agent turns](./design/agent-turns.md): the design behind replies the agent starts on its own, and how each run maps to one message.
- [Ask-user modal](./design/ask-user-modal.md): a spec for a tool-call-based question flow. It was not implemented as written: the shipped path intercepts `OpenClaw needs input:` text.
