"""
title: Stub model
description: Web Push E2E only. A plain model: Open WebUI's own chat.finished (source api) is the one to send.
"""
REPLY = "STUB-SECRET-REPLY this text must never reach a push payload"


class Pipe:
    def pipe(self, body: dict):
        return REPLY
