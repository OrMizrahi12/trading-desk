"""Asking Claude for the decision. `claude -p` is Claude Code's non-interactive "print" mode: one question, one answer."""

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Protocol

from .decision import SCHEMA

DESK_DIR = Path(__file__).parent.parent
INSTRUCTIONS = DESK_DIR / "CLAUDE.md"  # the desk constitution: the only instructions the trader gets
PROMPT = "Decide for the closed bar in the JSON snapshot on stdin. Answer only in the required schema."


class ModelFailure(RuntimeError):
    """The model gave no usable answer (`kind` says why: timeout, exit_code, bad_output, ...). The desk then waits."""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind


class Model(Protocol):
    def decide(self, snapshot: dict, timeout_s: float) -> dict: ...


def instructions_hash() -> str:
    return hashlib.sha1(INSTRUCTIONS.read_bytes()).hexdigest()[:12]


class ClaudeModel:
    """Runs `claude -p` in the desk folder with the decision JSON schema. No tools: it can only answer."""

    def __init__(self, *, cwd: Path = DESK_DIR, run=subprocess.run, executable: str = "claude", model: str | None = None):
        self.cwd, self.run, self.executable, self.model = cwd, run, executable, model

    def command(self) -> list[str]:
        cmd = [self.executable, "-p", PROMPT, "--output-format", "json", "--json-schema", json.dumps(SCHEMA),
               "--tools", "", "--no-session-persistence", "--system-prompt", INSTRUCTIONS.read_text(),
               "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
               "--setting-sources", ""]  # no settings or CLAUDE.md files at all: the system prompt is the whole instructions
        return cmd + (["--model", self.model] if self.model else [])

    def decide(self, snapshot: dict, timeout_s: float) -> dict:
        if timeout_s <= 0:
            raise ModelFailure("timeout", "no time left in the bar's budget")
        try:
            done = self.run(self.command(), input=json.dumps(snapshot, default=str), capture_output=True, text=True,
                            timeout=timeout_s, cwd=self.cwd)
        except subprocess.TimeoutExpired:
            raise ModelFailure("timeout", f"no answer within {timeout_s:.0f}s") from None
        except OSError as exc:
            raise ModelFailure("not_started", str(exc)) from None
        if done.returncode != 0:
            raise ModelFailure("exit_code", f"{done.returncode}: {done.stderr.strip()[:300]}")
        try:
            out = json.loads(done.stdout)
        except ValueError:
            raise ModelFailure("bad_output", f"stdout is not JSON: {done.stdout[:200]!r}") from None
        if not isinstance(out, dict) or out.get("is_error"):
            raise ModelFailure("error", str(out.get("result", out))[:300] if isinstance(out, dict) else "not an object")
        if "structured_output" not in out:
            raise ModelFailure("bad_output", "no structured_output in the answer")
        return out["structured_output"]
