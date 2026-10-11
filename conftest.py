"""Let the real pydantic win before any suite stubs it.

Three unit suites (test_pipe_unit, test_status_action_unit, test_devcoord_unit)
stub pydantic, websockets and cryptography so they run on a bare machine, each
behind `if "<name>" not in sys.modules`. Standalone that is correct. Under
pytest every suite shares one interpreter, so whichever module is collected
first decides what all the others see.

That ordering broke CI: test_status_action_unit sorts before
test_thinking_unit, its pydantic stub carries only BaseModel and Field, and the
thinking filter imports create_model. The error named pydantic at "unknown
location", which reads like a broken install rather than a stub, so it is worth
naming here.

Importing the genuine pydantic at collection time makes those guards see the
real thing and decline to stub. On a machine that genuinely lacks it nothing is
imported and the stubs still apply exactly as before.

Deliberately pydantic only. The websockets and cryptography stubs are load
bearing: real websockets resolves `websockets.exceptions` through a lazy import
shim that the stubbed transport tests do not drive, so hoisting it here trades
one broken suite for another. Nothing needs the real transport to be present.
"""

try:
    import pydantic  # noqa: F401
except ModuleNotFoundError as exc:
    if exc.name != "pydantic":
        raise
    # not installed here: the per-suite stub is the fallback

