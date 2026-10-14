# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built openclaw_pipe.py directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------


import asyncio
import json
import html
import os
import uuid
import logging
import base64
import time
import threading
import sys
import hashlib
import contextlib
import copy
import re
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

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def pipe_log(*args):
    """Log to stdout so it appears in OWUI's backend logs."""
    print(f"[openclaw-pipe] {' '.join(str(a) for a in args)}", flush=True)

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

# The event loop keeps only a weak reference to a task, so a fire-and-forget
# asyncio.create_task() can be garbage-collected before it finishes and its
# work silently never happens. Background work goes through spawn_background,
# which holds the reference until the task is done.
_BACKGROUND_TASKS: set = set()


def spawn_background(coro):
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task
