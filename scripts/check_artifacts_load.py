#!/usr/bin/env python3
"""Load each distributed artifact the way Open WebUI's plugin loader does.

Open WebUI execs a Function's flat file into a fresh module and introspects a
top-level Pipe/Action/Filter class, then instantiates it and its Valves. This
catches an artifact that imports and instantiates cleanly as fragments under
src/ but not as one standalone file.
"""

import sys
import types

ARTIFACTS = (
    ("openclaw_pipe", "openclaw_pipe.py", "Pipe"),
    ("openclaw_status_action", "openclaw_status_action.py", "Action"),
    ("openclaw_thinking_filter", "openclaw_thinking_filter.py", "Filter"),
)


def main() -> int:
    for name, path, cls_name in ARTIFACTS:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
        with open(path, encoding="utf-8") as fh:
            exec(compile(fh.read(), path, "exec"), mod.__dict__)
        cls = getattr(mod, cls_name, None)
        assert cls is not None, f"{path}: no top-level {cls_name}"
        instance = cls()
        valves = getattr(instance, "valves", None) or cls.Valves()
        schema = type(valves).model_json_schema()
        print(f"{path}: {cls_name} loaded and instantiated, "
              f"{len(schema.get('properties', {}))} valves")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
