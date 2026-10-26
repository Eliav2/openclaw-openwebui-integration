---
name: "openclaw-owui-media"
description: "Show an image or file in the Open WebUI chat by emitting a MEDIA: line with the file's absolute path on the Gateway host, or a bare filename."
---

# Sending an image or file to Open WebUI

The OpenClaw ⇄ Open WebUI pipe can attach a file to your reply so it renders
inline in the chat. Any model can do it; no shell is needed.

## How to send

Emit a line containing only `MEDIA:` and the file's absolute path on the
Gateway host, where you run:

```
MEDIA:/home/me/output/chart.png
```

That is all. The Gateway stores the file with your reply, and the pipe fetches
it from there and shows it inline once your reply ends. Use this when a tool
told you where it saved a file (an image tool, a screenshot).

## Bare filenames

A bare filename works too, for a file already in the pipe's media directory:

```
MEDIA:chart.png
```

If your deployment ships an upload helper script, it puts the file there and
prints that exact line. It is optional: use it only if you have a shell and
want the image to show while you are still writing. Otherwise use the absolute
path.

## Notes

- One directive per line. Several files means several `MEDIA:` lines.
- The filename needs an extension, otherwise the pipe treats it as ordinary
  prose. This is deliberate, so that writing about `MEDIA:` in a sentence does
  not turn the next word into a broken image.
- A `MEDIA:` line in a code block is left alone.
- An image the Gateway could not store (a path that does not exist on its host)
  is named in the reply, not dropped.
- Ordinary text can sit before and after the line, it is left alone.
