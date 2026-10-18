#!/usr/bin/env python3
"""Extract Open WebUI's Stop button templates from its frontend bundle, for the
writer's page intent hook (review 2af1 R15).

The hook tells Stop from Send now on the page. Both buttons' aria-labels are
translated (`t("Stop")`, `t("Send now")`), so a label match fails in any other
UI language. The Stop button's icon is not translated: the hook matches its
path. This script finds every button template the bundle labels `t("Stop")`
and stores them; test_pipe_unit.IntentHookStopIconTests checks the hook's icon
marker is in each one.

usage: extract_owui_stop_button.py <open-webui wheel | frontend dir> [--write]
"""
import json
import os
import re
import sys
import zipfile

FIXTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "fixtures",
                       "test_fixture_owui_stop_button.json")


def _chunks(src):
    if src.endswith(".whl"):
        z = zipfile.ZipFile(src)
        for n in z.namelist():
            if n.endswith(".js") and "/frontend/" in n:
                yield n.split("/frontend/", 1)[1], z.read(n).decode("utf-8", "ignore")
    else:
        for root, _dirs, files in os.walk(src):
            for f in files:
                if f.endswith(".js"):
                    p = os.path.join(root, f)
                    yield os.path.relpath(p, src), open(p, encoding="utf-8", errors="ignore").read()


def extract(src: str) -> list:
    out = []
    for name, text in _chunks(src):
        for m in re.finditer(r't\("Stop"\)', text):
            near = text[max(0, m.start() - 400):m.start()]
            made = re.findall(r"var (\w+)=([\w$]+)\(\)", near)
            if not made:
                continue
            fn = made[-1][1]
            tpl = re.search(re.escape(fn) + r"=[\w$]+\('(<button[^']*)'", text)
            if tpl and tpl.group(1) not in [t["template"] for t in out]:
                out.append({"chunk": name, "template": tpl.group(1)})
    return out


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    got = extract(args[0])
    if not got:
        sys.exit("no button template labelled t(\"Stop\") found")
    if "--write" in sys.argv:
        version = re.search(r"open_webui-([\d.]+)-", os.path.basename(args[0]))
        json.dump({"owui_version": version.group(1) if version else None, "templates": got},
                  open(FIXTURE, "w"), indent=1)
        open(FIXTURE, "a").write("\n")
    print(json.dumps(got, indent=1)[:2000])
