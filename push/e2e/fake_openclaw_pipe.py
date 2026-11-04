"""
title: Fake OpenClaw Pipe
description: Web Push E2E only. Stands in for the OpenClaw Pipe (function id openclaw_gateway): answers at once and, once the request is over, publishes chat.finished with the real Pipe's _publish_chat_finished, as the Pipe does for a reply that ended in its request.
"""
import importlib.util
import sys

REPLY = "PIPE-SECRET-REPLY this text must never reach a push payload"
PIPE_BUILD = "/mnt/pipe/openclaw_pipe.py"


def _real_pipe():
    mod = sys.modules.get("w1_real_openclaw_pipe")
    if mod is None:
        spec = importlib.util.spec_from_file_location("w1_real_openclaw_pipe", PIPE_BUILD)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules["w1_real_openclaw_pipe"] = mod
    return mod


class Pipe:
    def pipes(self):
        return [{"id": "fake", "name": "Fake OpenClaw"}]

    async def pipe(self, body: dict, __metadata__: dict = None, __user__: dict = None):
        meta = __metadata__ or {}
        ocp = _real_pipe()
        request = ocp._owui_request_task(meta.get("task_id"))
        chat_id, message_id, user_id = meta.get("chat_id"), meta.get("message_id"), (__user__ or {}).get("id")

        def ended(task):
            if not task.cancelled() and task.exception() is None:
                ocp.spawn_background(ocp._publish_chat_finished(chat_id, message_id, user_id))
        if request is not None:
            request.add_done_callback(ended)
        return REPLY
