# OpenClaw for Open WebUI

[![CI](https://github.com/Eliav2/openclaw-openwebui-integration/actions/workflows/ci.yml/badge.svg)](https://github.com/Eliav2/openclaw-openwebui-integration/actions/workflows/ci.yml)
[![Docs](https://img.shields.io/badge/docs-site-e4572e.svg)](https://eliav2.github.io/openclaw-openwebui-integration/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](./LICENSE)

<p align="center">
  <img src="./docs/img/demos/hero.gif" alt="A question typed in Open WebUI and sent; the agent's reply streams in, with a tool card for each command it runs, then a short summary" width="800">
</p>

Chat with your [OpenClaw](https://github.com/openclaw/openclaw) agents from Open WebUI, on your phone, and watch them work.
Tool calls, questions and sub-agents show up as real UI, not text.
It connects to your running Gateway over its native protocol; it is not an agent itself.

## Install in 30 seconds

On any machine that can reach Open WebUI (needs [uv](https://docs.astral.sh/uv/)):

```bash
git clone https://github.com/Eliav2/openclaw-openwebui-integration
cd openclaw-openwebui-integration
uv run install.py install --wizard
```

Then approve the device on the Gateway host (`openclaw devices approve <request-id>`; the installer does it for you if it ran there), pick **OpenClaw · Default** in Open WebUI and say hello. The [Quick start](https://eliav2.github.io/openclaw-openwebui-integration/install/quickstart) has a check at each step.

## Features

<table>
<tr>
<td width="50%" valign="middle">

### Live tool cards

Every command, read and search the agent runs is a card that fills in as it runs, so you watch the work happen instead of waiting on a wall of text.

[Docs →](https://eliav2.github.io/openclaw-openwebui-integration/features/chat#tool-cards)

</td>
<td width="50%">
<a href="https://eliav2.github.io/openclaw-openwebui-integration/features/chat#tool-cards"><picture><source srcset="./docs/img/demos/tile-tool-cards.gif" type="image/gif"><img src="./docs/img/tile-tool-cards.jpg" alt="A request to check the app before a release: two tool cards fill in as their commands run, then the first is opened to its command and output" width="100%"></picture></a>
</td>
</tr>
<tr>
<td width="50%" valign="middle">

### Questions you answer with a tap

When the agent needs a decision, you get a card with the choices instead of a wall of text, and the run goes on as soon as you tap one.

[Docs →](https://eliav2.github.io/openclaw-openwebui-integration/features/questions)

</td>
<td width="50%">
<a href="https://eliav2.github.io/openclaw-openwebui-integration/features/questions"><picture><source srcset="./docs/img/demos/tile-question.gif" type="image/gif"><img src="./docs/img/tile-question.jpg" alt="A Hebrew request to deploy: the agent asks a yes/no question as a card, Yes is tapped, and the reply goes on in Hebrew in the same message" width="100%"></picture></a>
</td>
</tr>
<tr>
<td width="50%" valign="middle">

### Talk to it while it works

Send a message while the agent is mid-run: it is in the chat at once, the running command is not interrupted, and your message gets its own answer when the run is done.

[Docs →](https://eliav2.github.io/openclaw-openwebui-integration/features/reliability#messages-sent-while-the-agent-works)

</td>
<td width="50%">
<a href="https://eliav2.github.io/openclaw-openwebui-integration/features/reliability#messages-sent-while-the-agent-works"><picture><source srcset="./docs/img/demos/tile-send-now.gif" type="image/gif"><img src="./docs/img/tile-send-now.jpg" alt="While a cache refresh runs, a second question is sent and Send now is clicked; the refresh finishes in its reply and the question gets its own answer" width="100%"></picture></a>
</td>
</tr>
<tr>
<td width="50%" valign="middle">

### Sub-agents you can follow

Work the agent hands to a sub-agent shows up as its own named card, and the sub-agent's result comes back to the chat by itself.

[Docs →](https://eliav2.github.io/openclaw-openwebui-integration/features/chat#sub-agent-cards)

</td>
<td width="50%">
<a href="https://eliav2.github.io/openclaw-openwebui-integration/features/chat#sub-agent-cards"><picture><source srcset="./docs/img/demos/tile-sub-agents.gif" type="image/gif"><img src="./docs/img/tile-sub-agents.jpg" alt="The agent starts a sub-agent named Cache history; its card is opened to the task and a Started line, and the sub-agent's result arrives as a new reply" width="100%"></picture></a>
</td>
</tr>
<tr>
<td width="50%" valign="middle">

### The whole run in one dialog

With the Status Action installed, one tap shows context used, rate limits, background commands and every sub-agent with its progress.

[Docs →](https://eliav2.github.io/openclaw-openwebui-integration/features/status#the-status-dialog)

</td>
<td width="50%">
<a href="https://eliav2.github.io/openclaw-openwebui-integration/features/status#the-status-dialog"><picture><source srcset="./docs/img/demos/status-dialog.gif" type="image/gif"><img src="./docs/img/tile-status-dialog.jpg" alt="The Status dialog over a chat: context and rate-limit bars, then two sub-agents running, and one opened to its task and progress" width="100%"></picture></a>
</td>
</tr>
<tr>
<td width="50%" valign="middle">

### Reload, and the reply is still there

Reload mid-reply, or open the chat on another device, and the answer so far is there, finishing in the same message.

[Docs →](https://eliav2.github.io/openclaw-openwebui-integration/features/reliability#reload-mid-reply)

</td>
<td width="50%">
<a href="https://eliav2.github.io/openclaw-openwebui-integration/features/reliability#reload-mid-reply"><picture><source srcset="./docs/img/demos/tile-reload.gif" type="image/gif"><img src="./docs/img/tile-reload.jpg" alt="A note being written; the page reloads part-way through and the same reply comes back and finishes" width="100%"></picture></a>
</td>
</tr>
<tr>
<td width="50%" valign="middle">

### Charts and files from the agent

Images and files the agent produces land in the reply itself, not as a path you have to go and find.

[Docs →](https://eliav2.github.io/openclaw-openwebui-integration/features/chat#images-and-files-from-the-agent)

</td>
<td width="50%">
<a href="https://eliav2.github.io/openclaw-openwebui-integration/features/chat#images-and-files-from-the-agent"><picture><source srcset="./docs/img/demos/tile-media.gif" type="image/gif"><img src="./docs/img/tile-media.jpg" alt="A request for the forecast chart: the reply shows the hourly temperature chart inline, with a line of advice under it" width="100%"></picture></a>
</td>
</tr>
</table>

**Also in the box:**

- **[Streaming without repeats](https://eliav2.github.io/openclaw-openwebui-integration/features/chat#streaming):** text appears as the Gateway sends it, and a runtime that resends the whole reply never shows it twice.
- **[Failed tools marked](https://eliav2.github.io/openclaw-openwebui-integration/features/chat#failed-tools):** a failed call is renamed with ❌ the moment it fails, and a card is never left spinning.
- **[Stop that closes the run](https://eliav2.github.io/openclaw-openwebui-integration/features/reliability#stop):** Stop ends the run and kills a command it was running, instead of leaving it to run on.
- **[Thinking level per chat](https://eliav2.github.io/openclaw-openwebui-integration/features/chat#thinking-level):** a composer toggle, with the thinking filter installed.
- **[Usage and goal line](https://eliav2.github.io/openclaw-openwebui-integration/features/chat#usage-and-goal-line):** context used and your rate limits under each reply.
- **[Every Gateway model](https://eliav2.github.io/openclaw-openwebui-integration/features/sessions-and-models#model-picker):** the model picker lists the models your Gateway allows, one session per chat.
- **[Replies the agent starts](https://eliav2.github.io/openclaw-openwebui-integration/features/agent-turns):** cron jobs, heartbeats and sub-agent results reply in the chat, live.
- **[Survives restarts](https://eliav2.github.io/openclaw-openwebui-integration/features/reliability#gateway-restarts):** an Open WebUI or Gateway restart does not cut a reply short: it continues in the same message.
- **[Owner users](https://eliav2.github.io/openclaw-openwebui-integration/features/owner-users):** for an Open WebUI that people other than you sign in to.

## Docs

Everything else is on the docs site: **[eliav2.github.io/openclaw-openwebui-integration](https://eliav2.github.io/openclaw-openwebui-integration/)**, from [why this exists](https://eliav2.github.io/openclaw-openwebui-integration/why) to [configuration](https://eliav2.github.io/openclaw-openwebui-integration/configuration), [troubleshooting](https://eliav2.github.io/openclaw-openwebui-integration/troubleshooting) and [versions](https://eliav2.github.io/openclaw-openwebui-integration/versions). For agents: [llms.txt](https://eliav2.github.io/openclaw-openwebui-integration/llms.txt) and [llms-full.txt](https://eliav2.github.io/openclaw-openwebui-integration/llms-full.txt). The same pages are in [docs/](./docs/index.md) here.

## Security, license, thanks

- **Security:** the integration drives your agent with its full privileges, so anyone who can chat with it can too. Read [Security](https://eliav2.github.io/openclaw-openwebui-integration/security) before you open Open WebUI to others, and report issues as [SECURITY.md](./SECURITY.md) describes.
- **License:** MIT, see [LICENSE](./LICENSE).
- **Thanks** to [cfullelove's gist](https://gist.github.com/cfullelove/7c6fa74e16d0a8f355e6d5ddb6d8e5fb), the proof of concept this grew from, and to [Open WebUI](https://openwebui.com/) and [OpenClaw](https://github.com/openclaw/openclaw).
