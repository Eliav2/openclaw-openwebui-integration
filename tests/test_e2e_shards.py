"""The E2E shards cover every E2E test once (ELI-246, e2e/shards.py).

The E2E job runs one stack per shard. A test missing from every shard would
never run in CI and nothing would say so; this is what says so.
"""

import importlib.util
import os

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "e2e", "shards.py")
_spec = importlib.util.spec_from_file_location("e2e_shards", _PATH)
shards = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shards)

TESTS = ["a.py::test_one", "a.py::test_two", "b.py::test_crash1", "b.py::test_crash2", "b.py::test_crash3"]
CRASHES = {"b.py::test_crash1", "b.py::test_crash2", "b.py::test_crash3"}


def test_every_e2e_test_is_in_exactly_one_shard_and_the_workflow_runs_every_shard():
    tests, crashes = shards.collect()
    assert len(tests) > 70, f"collected only {len(tests)} E2E tests: is e2e/ where shards.py looks?"
    assert len(crashes) == 3, f"the Gateway-crash tests: {sorted(crashes)}"
    assert shards.problems(shards.SHARDS, tests, crashes, shards.matrix()) == []


def test_a_test_in_no_shard_is_named():
    got = shards.problems({"1": TESTS[:1] + TESTS[2:4], "2": TESTS[4:]}, TESTS, CRASHES, ["1", "2"])
    assert got == ["in no shard: a.py::test_two"]


def test_a_test_in_two_shards_is_named():
    got = shards.problems({"1": TESTS[:4], "2": TESTS[1:2] + TESTS[4:]}, TESTS, CRASHES, ["1", "2"])
    assert got == ["in 2 shards: a.py::test_two"]


def test_a_shard_entry_that_is_not_a_test_is_named():
    got = shards.problems({"1": TESTS[:4], "2": TESTS[4:] + ["a.py::test_gone"]}, TESTS, CRASHES, ["1", "2"])
    assert got == ["not a test: a.py::test_gone"]


def test_a_third_gateway_crash_in_one_shard_is_refused():
    got = shards.problems({"1": TESTS}, TESTS, CRASHES, ["1"])
    assert len(got) == 1 and got[0].startswith("shard 1 crashes the Gateway 3 times"), got


def test_a_shard_the_workflow_does_not_run_is_named():
    got = shards.problems({"1": TESTS[:4], "2": TESTS[4:]}, TESTS, CRASHES, ["1"])
    assert got == ["the workflow's shard matrix ['1'] is not the shards ['1', '2']"]


def test_the_shard_plan_says_which_suites_a_shard_runs(capsys, monkeypatch):
    import runpy
    monkeypatch.setattr("sys.argv", ["shards.py", "2"])
    runpy.run_path(_PATH, run_name="__main__")
    assert capsys.readouterr().out.split() == ["e2e=true", "browser=false"]
