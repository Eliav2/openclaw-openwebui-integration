# Repo rule: no legacy paths

This repo does not carry backwards-compatibility shims or deprecation periods.

- When replacing a path, config key, or behavior: delete the old one in the *same* change that adds the new one. Do not leave both live "for now."
- No deprecation warnings, no dual-write/dual-read shims, no feature flags kept around just to ease a transition.
- If a caller (internal or external) still needs the old path, that's a reason to coordinate the change, not a reason to keep a shim.

Note: CodeRabbit on this repo is configured by Eliav directly — do not install, configure, or enable it.

# Repo rule: docs and screenshots ship with the change

- A PR that changes behavior, a valve or config key, install steps, or anything a user sees updates `README.md` and `docs/` in the same PR. A stale doc is a bug in that PR, not a follow-up.
- A PR that changes what the UI shows re-captures the affected images in `docs/img/` (light and dark) with `.github/workflows/screenshots.yml` on a runner, and puts Before/After screenshots in the PR body.
- When nothing user-facing changed, the PR body says so in one line ("Docs: no change needed, because ..."), so the reviewer can check the claim.
- `test_docs_unit.py` must pass: links, image budgets, and the wording rules.
