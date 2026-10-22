"""The PreToolUse hook (desk/hook.py) and the settings that load it. No IB, no network."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import LIMITS
from desk import hook
from desk.hook import Deny, evaluate

PROJECT = Path(__file__).resolve().parent.parent
T = "mcp__trading-desk__"


@pytest.fixture
def env(tmp_path):
    limits = tmp_path / "risk_limits.yaml"
    limits.write_text(LIMITS)
    (tmp_path / "state").mkdir()
    return limits, tmp_path / "state"


def call(env, tool, args=None, **kw):
    limits, state = env
    return evaluate({"tool_name": tool, "tool_input": args or {}}, limits_path=limits, stop_root=state, **kw)


def buy(**over):
    return {"size_r": 0.5, "stop": 597.0, "target": 606.0, "reason": "test", **over}


# -- layer 1: every MCP trade call is checked against the limits file --------------------------------


def test_a_sane_bracket_and_every_read_tool_pass(env):
    call(env, T + "place_bracket", buy())
    call(env, T + "place_bracket", buy(target=None, no_target_reason="trend day, no level in sight"))
    for tool in hook.READ_TOOLS:
        call(env, T + tool, {})


@pytest.mark.parametrize("over", [{"size_r": 1.5}, {"size_r": 0}, {"size_r": True}, {"size_r": "1"}, {"stop": 0}, {"stop": float("nan")},
                                  {"target": 590.0}, {"target": None}, {"target": None, "no_target_reason": "  "}])
def test_a_bracket_that_breaks_the_rules_is_denied(env, over):
    with pytest.raises(Deny):
        call(env, T + "place_bracket", buy(**over))


def test_the_limits_file_decides_and_a_broken_one_means_no_buys(env):
    limits, state = env
    limits.write_text(LIMITS.replace("SPY:", "QQQ:"))
    with pytest.raises(Deny, match="no limits for SPY"):
        call(env, T + "place_bracket", buy())
    for broken in ("", "symbols: [", "symbols: {SPY: {max_shares: 0, max_position_value: 1}}\nmax_trades_per_day: 1\ndaily_loss_limit: 1"):
        limits.write_text(broken)
        with pytest.raises(Deny, match="cannot be read"):
            call(env, T + "place_bracket", buy())
    limits.unlink()
    with pytest.raises(Deny, match="cannot be read"):
        call(env, T + "place_bracket", buy())


def test_the_stop_file_blocks_buys_but_never_exits(env):
    _, state = env
    (state / "STOP").write_text("kill switch")
    with pytest.raises(Deny, match="STOP file"):
        call(env, T + "place_bracket", buy())
    call(env, T + "close_position", {"reason": "get out"})
    call(env, T + "cancel_order", {"order_id": 7})
    call(env, T + "modify_order", {"order_id": 7, "new_price": 598.0})


def test_exits_pass_even_when_the_limits_file_is_gone(env):
    limits, _ = env
    limits.unlink()
    call(env, T + "close_position", {"reason": "get out"})


def test_unknown_trade_tools_and_lookalike_servers_are_denied(env):
    with pytest.raises(Deny, match="unknown MCP tool"):
        call(env, T + "place_market_order", {"size_r": 1})
    with pytest.raises(Deny, match="another name"):
        call(env, "mcp__plugin_x_trading-desk__place_bracket", buy())
    call(env, "mcp__claude_ai_Gmail__search_threads", {})  # other servers are none of this hook's business


def test_garbage_input_is_denied():
    with pytest.raises(Deny):
        evaluate({"tool_name": T + "place_bracket", "tool_input": "x"})
    with pytest.raises(Deny):
        evaluate({})


def run_hook(stdin: str, tmp_path):
    """The hook as Claude Code runs it: a process fed JSON on stdin."""
    return subprocess.run([sys.executable, "-m", "desk.hook"], input=stdin, capture_output=True, text=True, cwd=PROJECT, timeout=60)


def test_the_hook_process_denies_with_json_and_fails_closed_on_garbage(tmp_path):
    bad = run_hook(json.dumps({"tool_name": T + "place_bracket", "tool_input": buy(size_r=5)}), tmp_path)
    out = json.loads(bad.stdout)["hookSpecificOutput"]  # the reason may be size_r or the real lab's STOP file: both deny
    assert bad.returncode == 0 and out["hookEventName"] == "PreToolUse" and out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"].startswith("Desk guardrail:")
    crash = run_hook("not json at all", tmp_path)
    assert crash.returncode == 2 and "blocked" in crash.stderr  # exit 2 is what makes Claude Code block the call
    fine = run_hook(json.dumps({"tool_name": T + "get_quote", "tool_input": {}}), tmp_path)
    assert fine.returncode == 0 and fine.stdout == ""


# -- layer 2: only the person edits the limits file ---------------------------------------------------


@pytest.mark.parametrize("tool,key", [("Edit", "file_path"), ("Write", "file_path"), ("MultiEdit", "file_path"), ("NotebookEdit", "notebook_path")])
def test_edit_tools_cannot_touch_the_guarded_files(env, tool, key):
    limits, _ = env
    for target in (limits, hook.DEFAULT_LIMITS, PROJECT / "desk/hook.py", PROJECT / ".claude/settings.json",
                   PROJECT / ".claude/settings.local.json", PROJECT / ".mcp.json", "desk/../.mcp.json", "elsewhere/risk_limits.yaml"):
        with pytest.raises(Deny, match="guardrails"):
            call(env, tool, {key: str(target)})
    call(env, tool, {key: str(PROJECT / "README.md")})


def test_a_symlink_to_the_limits_file_is_still_the_limits_file(env, tmp_path):
    limits, _ = env
    link = tmp_path / "innocent.txt"
    link.symlink_to(limits)
    with pytest.raises(Deny):
        call(env, "Write", {"file_path": str(link)}, )


@pytest.mark.parametrize("cmd", [
    "sed -i 's/10/1000/' ~/trading-lab/live/risk_limits.yaml", "echo 'x' > risk_limits.yaml", "cat risk_limits.yaml > /tmp/a; rm risk_limits.yaml",
    "cp /tmp/mine.yaml ~/trading-lab/live/risk_limits.yaml", "python -c \"open('risk_limits.yaml','w')\"", "cat risk_limits.yaml | tee risk_limits.yaml",
    "rm .claude/settings.json", "git checkout -- .mcp.json", "echo >> desk/hook.py", "cat $(echo risk_limits.yaml)", "cat 'risk_limits.yaml"])
def test_shell_commands_that_write_guarded_files_are_denied(env, cmd):
    with pytest.raises(Deny, match="guarded file"):
        call(env, "Bash", {"command": cmd})


def test_looking_at_guarded_files_and_ordinary_commands_are_fine(env):
    for cmd in ("cat ~/trading-lab/live/risk_limits.yaml", "grep max_shares risk_limits.yaml", "ls -l .claude/settings.json",
                "uv run pytest", "git status", "git add -A"):
        call(env, "Bash", {"command": cmd})


# -- the wiring: settings.json really loads the hook and denies the edits -------------------------------


def settings():
    return json.loads((PROJECT / ".claude/settings.json").read_text())


def test_settings_json_wires_the_hook_to_every_tool_it_guards():
    import re
    entries = settings()["hooks"]["PreToolUse"]
    assert len(entries) == 1
    matcher = re.compile(entries[0]["matcher"])
    for tool in (T + "place_bracket", T + "close_position", "Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"):
        assert matcher.fullmatch(tool), tool
    cmd = entries[0]["hooks"][0]
    assert cmd["type"] == "command" and "-m desk.hook" in cmd["command"] and "CLAUDE_PROJECT_DIR" in cmd["command"]


def test_settings_json_denies_edits_of_the_guarded_files():
    deny = settings()["permissions"]["deny"]
    for rule in ("Edit(~/trading-lab/live/risk_limits.yaml)", "Edit(**/risk_limits.yaml)", "Edit(/.claude/settings*.json)",
                 "Edit(/desk/hook.py)", "Edit(/.mcp.json)"):
        assert rule in deny


def test_the_guarded_set_matches_the_deny_rules():
    """If a file is added to protected_paths but not to settings.json (or the reverse), this fails."""
    from fnmatch import fnmatch
    patterns = [r[len("Edit("):-1] for r in settings()["permissions"]["deny"]]
    for path in hook.protected_paths():
        rel = "/" + path.relative_to(PROJECT).as_posix() if path.is_relative_to(PROJECT) else "~/" + path.relative_to(Path.home()).as_posix()
        assert any(fnmatch(rel, p) or fnmatch(path.as_posix(), p) for p in patterns), path
