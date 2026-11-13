# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built openclaw_pipe.py directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------


import asyncio
import errno
import json
import html
import math
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

from pydantic import BaseModel, Field, create_model

# The chat and run a log line belongs to (ELI-368). Each reply opens a
# log_scope with its chat (Pipe.pipe) and each run inside it another with no
# run yet (_PipeTurn.run); the run id is set once known (_PipeTurn.our_run_id).
# The scope's end puts the caller's ids back, so a task that consumes replies
# one after another never logs one reply's ids on the next. asyncio copies the
# context into every task a reply starts, so the turn writer and other
# background work log under the reply's ids, and two replies at once (two
# tasks) never see each other's.
_LOG_CHAT = contextvars.ContextVar("pipe_log_chat", default=None)
_LOG_RUN = contextvars.ContextVar("pipe_log_run", default=None)


@contextlib.contextmanager
def log_scope(chat_id=None, run_id=None):
    """pipe_log lines inside carry these ids (None: no tag); the caller's ids
    come back when the block ends."""
    tokens = ((_LOG_CHAT, _LOG_CHAT.set(chat_id)), (_LOG_RUN, _LOG_RUN.set(run_id)))
    try:
        yield
    finally:
        for var, token in tokens:
            try:
                var.reset(token)
            except ValueError:
                # Closed from another context (an async generator finalized by
                # another task): our values were never set there, so nothing
                # to undo.
                pass


def log_run(run_id):
    """The run this scope's lines belong to from now on (None: no run)."""
    _LOG_RUN.set(run_id)


def pipe_log(*args):
    """Log to stdout so it appears in OWUI's backend logs.

    A line logged inside a reply ends with `chat_id=<id> runId=<id>` (a run id
    the line already names is not repeated): central logs index both, so
    `logtrace <chat id>` finds the whole turn.
    """
    msg = ' '.join(str(a) for a in args)
    chat, run = _LOG_CHAT.get(), _LOG_RUN.get()
    tags = []
    # Only the tag itself counts as already there for the chat: a run id like
    # msg-<chat id>-... contains the chat id, and a search for chat_id=<id>
    # must still find its line.
    if chat and f"chat_id={chat}" not in msg:
        tags.append(f"chat_id={chat}")
    if run and run not in msg:
        tags.append(f"runId={run}")
    print(f"[openclaw-pipe] {' '.join([msg, *tags])}", flush=True)



# The event loop holds tasks weakly, so a bare create_task() can be collected
# before it runs. spawn_background keeps a reference until the task is done.
_BACKGROUND_TASKS: set = set()


def spawn_background(coro):
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task
