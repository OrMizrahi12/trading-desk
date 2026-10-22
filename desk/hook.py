"""PreToolUse guard for an interactive Claude Code session in this folder (wired up in `.claude/settings.json`).

A PreToolUse hook is a command Claude Code runs before every matching tool call; it reads the call as JSON on stdin and
can deny it. The model cannot argue with it: it is not part of the conversation. Two jobs:

1. Every MCP trade tool call (`mcp__trading-desk__*`) is checked against the lab's risk limits file
   (`risk_limits.yaml`) and the desk's hard rules. This repeats, before the order exists, what the MCP server and the
   lab's Broker check again afterwards. Exits (`close_position`, `cancel_order`) are never blocked by the limits file.
2. Nothing but the person may change the limits file, this hook, the settings that load it, or `.mcp.json`: edit tools
   are denied on those paths and a shell command that names one is denied unless it is plainly read-only.

It fails closed: an unreadable limits file, unreadable input or any crash denies the call (exit code 2 blocks).
The Bash check is a best-effort tripwire, not a sandbox: only file permissions or a separate OS user truly stop a shell.

Terms:
- R: 1% of net liquidation (total account value), the loss if the stop is hit. `size_r` is the trade's size in R.
- stop: a resting order that sells at your loss limit. target: a resting order that sells at your profit goal.
"""

import json
import math
import re
import shlex
import sys
from pathlib import Path

from . import lab  # noqa: F401
from live.risk import DEFAULT_LIMITS, load_limits, stop_path

PROJECT = Path(__file__).resolve().parent.parent
SERVER = "trading-desk"
PREFIX = f"mcp__{SERVER}__"
SYMBOL = "SPY"
MAX_SIZE_R = 1.0  # CLAUDE.md: never above 1R
READ_TOOLS = {"get_quote", "get_bars", "get_indicators", "get_positions", "get_open_orders", "get_account_summary",
              "search_journal"}
EXIT_TOOLS = {"close_position", "cancel_order"}  # reduce risk: always allowed, even if the limits file is broken
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
READ_ONLY_COMMANDS = {"cat", "head", "tail", "less", "more", "grep", "wc", "ls", "stat", "diff", "file", "sha1sum", "sha256sum"}
SHELL_SYNTAX = re.compile(r"[;&|<>`$()\n]")
# a shell command naming any of these is treated as touching a protected file
PROTECTED_NAMES = re.compile(r"risk_limits|\.claude/settings|desk/hook|\.mcp\.json")


class Deny(Exception):
    """The call is refused; the message is shown to the model."""


def protected_paths(limits_path: Path = DEFAULT_LIMITS, project: Path = PROJECT) -> set[Path]:
    paths = {limits_path, project / "desk" / "hook.py", project / ".mcp.json",
             project / ".claude" / "settings.json", project / ".claude" / "settings.local.json"}
    return {p.resolve() for p in paths}


def is_protected(raw: str, *, limits_path: Path = DEFAULT_LIMITS, project: Path = PROJECT) -> bool:
    path = Path(raw).expanduser()
    path = (project / path if not path.is_absolute() else path).resolve()
    return path in protected_paths(limits_path, project) or path.name.startswith("risk_limits")


def _number(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise Deny(f"{name} must be a number, got {value!r}")
    return float(value)


def check_trade(tool: str, args: dict, *, limits_path: Path, stop_root: Path | None) -> None:
    """Raise Deny unless this MCP trade call respects the limits file and the desk's hard rules."""
    if tool in EXIT_TOOLS:
        return
    if tool == "modify_order":
        if _number(args.get("new_price"), "new_price") <= 0:
            raise Deny("new_price must be above 0")
        return
    if tool != "place_bracket":
        raise Deny(f"unknown MCP tool {tool!r}: only the desk's own tools are allowed (default deny)")
    try:
        limits = load_limits(limits_path.read_text())
    except Exception as exc:  # missing file, bad YAML, bad numbers: the lab treats all of them as an error, never a default
        raise Deny(f"the risk limits file {limits_path} cannot be read ({type(exc).__name__}: {exc}); no new buys") from exc
    if stop_root is not None and stop_path(stop_root).exists():
        raise Deny("the STOP file (the lab's kill switch) exists: no new buys")
    if SYMBOL not in limits.symbols:
        raise Deny(f"no limits for {SYMBOL} in {limits_path.name} (default deny)")
    size_r = _number(args.get("size_r"), "size_r")
    if not 0 < size_r <= MAX_SIZE_R:
        raise Deny(f"size_r {size_r} must be above 0 and at most {MAX_SIZE_R}R (1R = 1% of net liquidation)")
    stop = _number(args.get("stop"), "stop")
    if stop <= 0:
        raise Deny("a buy needs a stop price above 0")
    if args.get("target") is None:
        if not str(args.get("no_target_reason") or "").strip():
            raise Deny("a target is required unless no_target_reason explains why there is none")
    elif _number(args["target"], "target") <= stop:
        raise Deny("the target must be above the stop")
    # Share and dollar caps (max_shares, max_position_value), trades per day and the daily loss limit need the price and
    # the account, so the MCP server and the lab's Broker enforce them; the hook only proves the file is valid and has SPY.


def check_edit(args: dict, *, limits_path: Path, project: Path) -> None:
    for key in ("file_path", "notebook_path", "path"):
        raw = args.get(key)
        if isinstance(raw, str) and is_protected(raw, limits_path=limits_path, project=project):
            raise Deny(f"{raw} is part of the desk's guardrails (limits file, hook, settings or .mcp.json): "
                       "only the person may change it, outside this session")


def check_bash(command: str) -> None:
    if not PROTECTED_NAMES.search(command):
        return
    try:
        words = shlex.split(command)
    except ValueError:
        words = []
    if words and words[0] in READ_ONLY_COMMANDS and not SHELL_SYNTAX.search(command):
        return  # `cat risk_limits.yaml` and friends: looking is fine
    raise Deny("this shell command names a guarded file (risk limits, hook, settings or .mcp.json); only read-only commands "
               "such as `cat` are allowed on them. The person must make this change outside the session")


def evaluate(event: dict, *, limits_path: Path = DEFAULT_LIMITS, project: Path = PROJECT,
             stop_root: Path | None = None) -> None:
    """Raise Deny if the PreToolUse `event` must be blocked; return None to let it through."""
    name, args = event.get("tool_name"), event.get("tool_input")
    if not isinstance(name, str) or not isinstance(args, dict):
        raise Deny("unreadable tool call")
    if name.startswith(PREFIX):
        tool = name[len(PREFIX):]
        if tool not in READ_TOOLS:
            check_trade(tool, args, limits_path=limits_path, stop_root=stop_root)
    elif name.startswith("mcp__") and SERVER in name:
        raise Deny(f"{name!r} looks like the desk's server under another name")
    elif name in EDIT_TOOLS:
        check_edit(args, limits_path=limits_path, project=project)
    elif name == "Bash":
        check_bash(str(args.get("command", "")))


def main() -> int:
    try:
        evaluate(json.load(sys.stdin), stop_root=_stop_root())
    except Deny as exc:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                  "permissionDecisionReason": f"Desk guardrail: {exc}"}}))
        return 0
    except BaseException as exc:  # fail closed: exit code 2 blocks the call and shows stderr to the model
        print(f"Desk guardrail crashed ({type(exc).__name__}: {exc}); the call is blocked", file=sys.stderr)
        return 2
    return 0


def _stop_root() -> Path:
    from live.config import STATE_DIR
    return STATE_DIR


if __name__ == "__main__":
    sys.exit(main())
