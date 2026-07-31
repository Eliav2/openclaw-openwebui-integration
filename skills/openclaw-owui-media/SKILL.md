---
name: "openclaw-owui-media"
description: "Show an image or file in the Open WebUI chat by emitting a MEDIA: line with a bare filename."
---

# Sending an image or file to Open WebUI

The OpenClaw ⇄ Open WebUI pipe can attach a file to your reply so it renders
inline in the chat.

## How to send

Put the file in the pipe's media directory, then emit a line containing only:

```
MEDIA:diagram.png
```

The pipe uploads it through Open WebUI's own Files API and attaches it to your
message. The user sees the image inline, not a link.

## The one rule that breaks people

**Use a bare filename. Never an absolute path.**

```
MEDIA:chart.png                     correct
MEDIA:/home/me/output/chart.png     silently drops
```

OpenClaw and Open WebUI usually run as separate containers with no shared
filesystem, so a path that is valid where you are running means nothing to the
pipe. A path-shaped `MEDIA:` line produces no image and no error, which makes it
very hard to notice you got it wrong.

If your deployment ships a helper script for uploading, use it: it puts the file
where the pipe can see it and prints the exact line to emit.

## Notes

- One directive per line. Several files means several `MEDIA:` lines.
- The filename needs an extension, otherwise the pipe treats it as ordinary
  prose. This is deliberate, so that writing about `MEDIA:` in a sentence does
  not turn the next word into a broken image.
- Ordinary text can sit before and after the line, it is left alone.
