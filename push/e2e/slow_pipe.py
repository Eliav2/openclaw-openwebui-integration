"""
title: Slow model
description: Web Push E2E only. A plain model that answers after a few seconds, so a message can arrive while its reply runs.
"""
import asyncio

REPLY = "SLOW-REPLY done"


class Pipe:
    async def pipe(self, body: dict):
        await asyncio.sleep(8)
        return REPLY
