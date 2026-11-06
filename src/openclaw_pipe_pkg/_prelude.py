# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built openclaw_pipe.py directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------


import asyncio
import errno
import json
import html
import os
import uuid
import base64
import time
import threading
import sys
import hashlib
import io
import contextlib
import contextvars
import copy
import functools
import re
import shlex
import signal
import mimetypes
import urllib.error
import urllib.request
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler
from dataclasses import dataclass, field

import ipaddress
import websockets
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend

from pydantic import BaseModel, Field

# The chat and run the current turn belongs to (ELI-368). pipe() sets the chat
# and the stream sets the run once captured; asyncio copies the context into
# every task the turn starts, so the turn writer and other background work log
# under the same ids, and two turns at once never see each other's.
_LOG_CHAT = contextvars.ContextVar("pipe_log_chat", default=None)
_LOG_RUN = contextvars.ContextVar("pipe_log_run", default=None)


def log_context(chat_id=None, run_id=None):
    """Name the chat and/or run that this task's pipe_log lines belong to."""
    if chat_id:
        _LOG_CHAT.set(chat_id)
    if run_id:
        _LOG_RUN.set(run_id)


def pipe_log(*args):
    """Log to stdout so it appears in OWUI's backend logs.

    A line logged inside a turn ends with `chat_id=<id> runId=<id>` (an id the
    line already names is not repeated): central logs index both, so
    `logtrace <chat id>` finds the whole turn.
    """
    msg = ' '.join(str(a) for a in args)
    chat, run = _LOG_CHAT.get(), _LOG_RUN.get()
    tags = [t for t, v in ((f"chat_id={chat}", chat), (f"runId={run}", run)) if v and v not in msg]
    print(f"[openclaw-pipe] {' '.join([msg, *tags])}", flush=True)



# The event loop holds tasks weakly, so a bare create_task() can be collected
# before it runs. spawn_background keeps a reference until the task is done.
_BACKGROUND_TASKS: set = set()


def spawn_background(coro):
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task
