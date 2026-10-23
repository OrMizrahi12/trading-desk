"""Asking Claude for the decision. `claude -p` is Claude Code's non-interactive "print" mode: one question, one answer."""

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .decision import SCHEMA

DESK_DIR = Path(__file__).parent.parent
INSTRUCTIONS = DESK_DIR / "CLAUDE.md"  # the desk constitution: the only instructions the trader gets
PROMPT = "Decide for the closed bar in the JSON snapshot on stdin. Answer only in the required schema."


@dataclass
class Session:
    """One Claude conversation, kept for the whole trading day. `resume` False = start it with `id`, True = continue it.

    `intro` is extra text for the first message of a new session (today's date, a summary of the day before).
    The model fills `instructions` (hash of the exact text it sent as system prompt) and `context` (token counts).
    """

    id: str
    resume: bool = False
    intro: str = ""
    instructions: str | None = None
    context: dict = field(default_factory=dict)


class ModelFailure(RuntimeError):
    """The model gave no usable answer (`kind` says why: timeout, exit_code, bad_output, ...). The desk then waits."""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind


class Model(Protocol):
    def decide(self, snapshot: dict, timeout_s: float, session: Session | None = None) -> dict: ...


def text_hash(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:12]


def instructions_hash() -> str:
    return hashlib.sha1(INSTRUCTIONS.read_bytes()).hexdigest()[:12]


def context_usage(out: dict) -> dict:
    """How big the conversation was on the last request: input + cached input tokens (what the model had to read).

    Cached tokens (cache_read = reused from an earlier call, cache_creation = newly stored) are still part of the context.
    """
    usage = out.get("usage") or {}
    last = (usage.get("iterations") or [usage])[-1]  # the final request; with no tools there is exactly one
    parts = {k: int(last.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}
    windows = [m.get("contextWindow") for m in (out.get("modelUsage") or {}).values() if m.get("contextWindow")]
    return {"tokens": sum(parts.values()), **parts, "output_tokens": int(last.get("output_tokens") or 0),
            "window": max(windows) if windows else None, "cost_usd": out.get("total_cost_usd")}


class ClaudeModel:
    """Runs `claude -p` in the desk folder with the decision JSON schema. No tools: it can only answer."""

    def __init__(self, *, cwd: Path = DESK_DIR, run=subprocess.run, executable: str = "claude", model: str | None = None):
        self.cwd, self.run, self.executable, self.model = cwd, run, executable, model

    def command(self, session: Session | None = None) -> list[str]:
        text = INSTRUCTIONS.read_text()  # read once: the hash we report is of exactly the text we send
        cmd = [self.executable, "-p", (session.intro + "\n\n" if session and session.intro else "") + PROMPT,
               "--output-format", "json", "--json-schema", json.dumps(SCHEMA),
               "--tools", "", "--system-prompt", text,
               "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
               "--setting-sources", ""]  # no settings or CLAUDE.md files at all: the system prompt is the whole instructions
        if session is None:
            cmd.append("--no-session-persistence")  # one question, no memory
        else:
            # Without "off", a resumed conversation silently keeps the system prompt of its first request, so the
            # hash we journal could differ from what the trader read. With "off" every request sends `text`.
            cmd += ["--system-prompt-snapshot", "off", "--resume" if session.resume else "--session-id", session.id]
            session.instructions = text_hash(text)
        return cmd + (["--model", self.model] if self.model else [])

    def decide(self, snapshot: dict, timeout_s: float, session: Session | None = None) -> dict:
        if timeout_s <= 0:
            raise ModelFailure("timeout", "no time left in the bar's budget")
        try:
            done = self.run(self.command(session), input=json.dumps(snapshot, default=str), capture_output=True, text=True,
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
        if session is not None and isinstance(out, dict):
            session.context = context_usage(out)  # even a bad answer cost context
            session.id = out.get("session_id") or session.id  # the id claude reports is the one to resume next time
        if not isinstance(out, dict) or out.get("is_error"):
            raise ModelFailure("error", str(out.get("result", out))[:300] if isinstance(out, dict) else "not an object")
        if "structured_output" not in out:
            raise ModelFailure("bad_output", "no structured_output in the answer")
        return out["structured_output"]
