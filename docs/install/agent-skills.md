Two features need your agent to know a convention: asking you a question as a dialog, and sending you an image or file. Two skills in this repo teach it both; install them on the Gateway host.

Applies to: Pipe

## Why the agent needs teaching

The integration provides the mechanism, but nothing tells your model it exists, so out of the box the agent will never use these:

| Feature | The agent must | Otherwise |
|---|---|---|
| [Ask-user dialog](../features/questions.md) | Start a line with `OpenClaw needs input:` | It calls its own ask tool, which never reaches you here |
| [Send an image or file](../features/chat.md) | Emit `MEDIA:<bare-filename>` | An absolute path silently drops, with no image and no error |

## The two skills

They live in [`skills/`](../../skills):

- [`openclaw-owui-ask-user`](../../skills/openclaw-owui-ask-user/SKILL.md): ask the user a question that pops a real dialog in Open WebUI, using the `OpenClaw needs input:` marker.
- [`openclaw-owui-media`](../../skills/openclaw-owui-media/SKILL.md): show an image or file in the chat by emitting a `MEDIA:` line with a bare filename.

## Install them

Install them **on the Gateway host**, which is where the agent runs and is not always where Open WebUI runs. In a clone of this repo on the Gateway host:

```bash
openclaw skills install ./skills/openclaw-owui-ask-user
openclaw skills install ./skills/openclaw-owui-media
```

Or let the installer do it, in the same checkout:

```bash
uv run install.py skills
```

It needs no Open WebUI credentials. It detects the local `openclaw` CLI and installs every skill under `skills/`. If the CLI isn't there, the Gateway is usually on another machine: it says so, installs nothing, and prints the commands to run there instead.

`install.py install --with-skills` is meant to do the same at the end of an install, but on the current version the installer exits before it reaches that step. Run `uv run install.py skills` on its own.

## Update them

`git pull`, then re-run the same command with overwrite turned on. In the repo checkout on the Gateway host:

```bash
uv run install.py skills --force
```

or, per skill:

```bash
openclaw skills install ./skills/openclaw-owui-ask-user --force
openclaw skills install ./skills/openclaw-owui-media --force
```

Without `--force`, a skill that is already installed fails with "already present? re-run with --force-skills to overwrite"; for the `skills` command, the flag is `--force`.

Overwriting is opt-in on purpose: OpenClaw keeps no history for skill installs, so an overwrite is unrecoverable, and a skill you wrote yourself should never be clobbered by an installer.

## Without the skills

If you would rather not install anything, put the two conventions in your agent's own instructions. The skills are only a convenient carrier.

On two machines, the media convention needs one more thing: the file has to reach the Pipe's media directory on the Open WebUI host. See [two hosts](./two-hosts.md#images-and-files-from-the-agent).
