"""ClaudeModel: how `claude -p` is called and every way it can fail. A fake subprocess runner, no real claude."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from desk.decision import SCHEMA
from desk.model import ClaudeModel, ModelFailure


def runner(stdout="", returncode=0, stderr="", raises=None):
    seen = {}

    def run(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        if raises:
            raise raises
        return SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)

    run.seen = seen
    return run


def reply(**kw):
    return json.dumps({"is_error": False, "result": "x", "structured_output": {"action": "wait"}, **kw})


def test_call_shape():
    run = runner(reply())
    assert ClaudeModel(run=run).decide({"bar": 1}, 60) == {"action": "wait"}
    cmd = run.seen["cmd"]
    assert cmd[:2] == ["claude", "-p"]
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == SCHEMA
    assert cmd[cmd.index("--tools") + 1] == ""  # no tools: it can only answer
    assert "--no-session-persistence" in cmd
    assert json.loads(run.seen["input"]) == {"bar": 1}  # the snapshot goes in on stdin
    assert run.seen["timeout"] == 60
    assert run.seen["cwd"].name == "trading-desk"


@pytest.mark.parametrize("run, kind", [
    (runner(raises=subprocess.TimeoutExpired("claude", 5)), "timeout"),
    (runner(raises=FileNotFoundError("claude")), "not_started"),
    (runner(returncode=1, stderr="boom"), "exit_code"),
    (runner("not json"), "bad_output"),
    (runner(json.dumps({"is_error": True, "result": "overloaded"})), "error"),
    (runner(json.dumps({"is_error": False, "result": "text only"})), "bad_output"),
])
def test_failures_raise_model_failure(run, kind):
    with pytest.raises(ModelFailure) as err:
        ClaudeModel(run=run).decide({}, 10)
    assert err.value.kind == kind


def test_no_time_left_is_a_timeout_without_calling_claude():
    run = runner(reply())
    with pytest.raises(ModelFailure) as err:
        ClaudeModel(run=run).decide({}, 0)
    assert err.value.kind == "timeout" and not run.seen
