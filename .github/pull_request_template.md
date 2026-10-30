## What this changes

<!-- One or two sentences. Link the issue it fixes, e.g. "Fixes #123". -->

## How it was tested

<!-- The tests you added or ran, and what you checked by hand. -->

## Before / After

<!-- A UI change: one Before and After pair from the Screenshots workflow, light theme, as
<img src="..." alt="..." width="320"> tags or ![alt](./file.png) images (a phone shot is 320 wide), uploaded with
gh pr edit --attach. The PR media check fails a PR
that touches the UI without an image. Nothing on screen changes: delete this section and add the
label no-visual-change. -->

- [ ] `uv run --with-requirements requirements-dev.txt pytest -q` passes
- [ ] Built files rebuilt with `python3 build.py --all` if `src/` changed
- [ ] Docs updated if users see the change
