"""Let the real pydantic win before any suite stubs it.

Under pytest the first suite collected decides the stubs for all, and
test_status_action_unit's pydantic stub lacks create_model. Pydantic only:
the websockets and cryptography stubs are load bearing."""

import os
import sys

# Suites import product modules from the repo root (pytest.ini) and the
# vendored Open WebUI handler from fixtures/, also standalone.
_TESTS = os.path.dirname(os.path.abspath(__file__))
for _path in (os.path.join(_TESTS, "fixtures"), os.path.dirname(_TESTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

try:
    import pydantic  # noqa: F401
except ModuleNotFoundError as exc:
    if exc.name != "pydantic":
        raise
    # not installed here: the per-suite stub is the fallback

