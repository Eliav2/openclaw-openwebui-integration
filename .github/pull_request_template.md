## What this changes

<!-- One or two sentences. Link the issue it fixes, e.g. "Fixes #123". -->

## How it was tested

<!-- The tests you added or ran, and what you checked by hand. -->

## Before / After

<!-- A UI change: a 2-column table, one row per change, with a short heading above each table saying
what changed. Light theme, from the Screenshots workflow; a phone shot is 320 wide:

### Notification shows the reply text
| Before | After |
|---|---|
| <img src="URL" alt="..." width="320"> | <img src="URL" alt="..." width="320"> |

gh pr edit --attach rewrites only ![alt](./file.png) references, not an <img> src: attach the files as
markdown images first, then move the uploaded URLs into the table (AGENTS.md has the steps). The PR
media check fails a PR that touches the UI without an image. Nothing on screen changes: delete this
section and add the label no-visual-change. -->

- [ ] `uv run --with-requirements requirements-dev.txt pytest -q` passes
- [ ] Built files rebuilt with `python3 build.py --all` if `src/` changed
- [ ] Docs updated if users see the change
