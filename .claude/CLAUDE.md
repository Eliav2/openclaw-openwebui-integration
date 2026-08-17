# Repo rule: no legacy paths

This repo does not carry backwards-compatibility shims or deprecation periods.

- When replacing a path, config key, or behavior: delete the old one in the *same* change that adds the new one. Do not leave both live "for now."
- No deprecation warnings, no dual-write/dual-read shims, no feature flags kept around just to ease a transition.
- If a caller (internal or external) still needs the old path, that's a reason to coordinate the change, not a reason to keep a shim.

Note: CodeRabbit on this repo is configured by Eliav directly — do not install, configure, or enable it.
