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
import re
import mimetypes
import urllib.error
import urllib.request
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler
from dataclasses import dataclass, field

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
