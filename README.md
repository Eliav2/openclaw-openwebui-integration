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

## What you get

- **[Live tool cards](https://eliav2.github.io/openclaw-openwebui-integration/features/chat#tool-cards)** for every command, read and search.
- **[Questions as dialogs](https://eliav2.github.io/openclaw-openwebui-integration/features/questions),** not walls of text.
- **[Status](https://eliav2.github.io/openclaw-openwebui-integration/features/status):** context, rate limits, background work, sub-agents.
- **[Every Gateway model](https://eliav2.github.io/openclaw-openwebui-integration/features/sessions-and-models)** in the model picker.
- **[Cron jobs and sub-agent results](https://eliav2.github.io/openclaw-openwebui-integration/features/agent-turns)** reply in the chat, live.
- **[Survives restarts](https://eliav2.github.io/openclaw-openwebui-integration/features/reliability)** and reconnects.

## Docs

Everything else is on the docs site: **[eliav2.github.io/openclaw-openwebui-integration](https://eliav2.github.io/openclaw-openwebui-integration/)**, from [why this exists](https://eliav2.github.io/openclaw-openwebui-integration/why) to [configuration](https://eliav2.github.io/openclaw-openwebui-integration/configuration), [troubleshooting](https://eliav2.github.io/openclaw-openwebui-integration/troubleshooting) and [versions](https://eliav2.github.io/openclaw-openwebui-integration/versions). For agents: [llms.txt](https://eliav2.github.io/openclaw-openwebui-integration/llms.txt) and [llms-full.txt](https://eliav2.github.io/openclaw-openwebui-integration/llms-full.txt). The same pages are in [docs/](./docs/index.md) here.

## Security, license, thanks

- **Security:** the integration drives your agent with its full privileges, so anyone who can chat with it can too. Read [Security](https://eliav2.github.io/openclaw-openwebui-integration/security) before you open Open WebUI to others, and report issues as [SECURITY.md](./SECURITY.md) describes.
- **License:** MIT, see [LICENSE](./LICENSE).
- **Thanks** to [cfullelove's gist](https://gist.github.com/cfullelove/7c6fa74e16d0a8f355e6d5ddb6d8e5fb), the proof of concept this grew from, and to [Open WebUI](https://openwebui.com/) and [OpenClaw](https://github.com/openclaw/openclaw).
