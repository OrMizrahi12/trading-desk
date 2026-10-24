"""Asking Claude for the decision. `claude -p` is Claude Code's non-interactive "print" mode: one question, one answer."""

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .decision import SCHEMA

DESK_DIR = Path(__file__).parent.parent
INSTRUCTIONS = DESK_DIR / "CLAUDE.md"  # the desk constitution: the system prompt, sent whole on every bar
PLUGIN_MANIFEST = DESK_DIR / ".claude-plugin" / "plugin.json"  # makes the desk folder a plugin so `--plugin-dir` finds the skills
SKILLS_DIR = DESK_DIR / ".claude" / "skills"  # the trader's know-how, loaded on demand with the Skill tool
PROMPT = "Decide for the closed bar in the JSON snapshot on stdin. Answer only in the required schema."


@dataclass
class Session:
    """One Claude conversation, kept for the whole trading day. `resume` False = start it with `id`, True = continue it.

    `intro` is extra text for the first message of a new session (today's date, a summary of the day before).
    The model fills `instructions` (hash of the exact text it sent as system prompt), `context` (token counts) and
    `skills` (the desk skills claude loaded with its Skill tool on this bar, in order; None = not known, e.g. a timeout).
    `full` is the bar's complete snapshot (the model may get only a delta; the team's roles keep no memory and need all of
    it) and `team` the team's record for the bar (views, tally, cost), both only used by team.TeamModel.
    """

    id: str
    resume: bool = False
    intro: str = ""
    instructions: str | None = None
    context: dict = field(default_factory=dict)
    skills: list[str] | None = None
    full: dict | None = None
    team: dict | None = None


class ModelFailure(RuntimeError):
    """The model gave no usable answer (`kind` says why: timeout, exit_code, bad_output, ...). The desk then waits."""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind


class Model(Protocol):
    def decide(self, snapshot: dict, timeout_s: float, session: Session | None = None) -> dict: ...


def text_hash(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:12]


def instruction_files() -> list[Path]:
    """Everything that shapes the trader's behaviour: the constitution, the plugin manifest and every skill file."""
    return [INSTRUCTIONS, PLUGIN_MANIFEST, *sorted(p for p in SKILLS_DIR.rglob("*") if p.is_file())]


def instructions_hash(constitution: str | None = None) -> str:
    """Fingerprint of `instruction_files()` (the journal records it, so a run names its exact instructions).

    `constitution` is the CLAUDE.md text exactly as sent, so the hash describes what the trader read."""
    digest = hashlib.sha1()
    for path in instruction_files():
        body = constitution.encode() if constitution is not None and path == INSTRUCTIONS else path.read_bytes()
        digest.update(path.relative_to(DESK_DIR).as_posix().encode() + b"\0" + body + b"\0")
    return digest.hexdigest()[:12]


def parse_stream(stdout: str) -> tuple[dict | None, list[str]]:
    """Split `--output-format stream-json` output into the final result event and the skills loaded on the way.

    The skills come from claude's own `Skill` tool calls (assistant `tool_use` blocks), not from the model's words.
    Lines that are not JSON are ignored; a stream with no result event gives None.
    """
    result, skills = None, []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "result":
            result = event
        elif event.get("type") == "assistant" and not event.get("parent_tool_use_id"):  # the trader's own calls only
            content = (event.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Skill":
                    skills.append(str((block.get("input") or {}).get("skill", "?")))
    return result, skills


def context_usage(out: dict) -> dict:
    """How big the conversation was on the last request: input + cached input tokens (what the model had to read).

    Cached tokens (cache_read = reused from an earlier call, cache_creation = newly stored) are still part of the context.
    """
    usage = out.get("usage") or {}
    last = (usage.get("iterations") or [usage])[-1]  # the final request (after any Skill loads)
    parts = {k: int(last.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}
    windows = [m.get("contextWindow") for m in (out.get("modelUsage") or {}).values() if m.get("contextWindow")]
    return {"tokens": sum(parts.values()), **parts, "output_tokens": int(last.get("output_tokens") or 0),
            "window": max(windows) if windows else None, "session_cost_usd": out.get("total_cost_usd")}  # claude's running total for the session


class ClaudeModel:
    """Runs `claude -p` in the desk folder with the decision JSON schema. Its only tool is Skill: it can load the
    desk's skills and answer, nothing else."""

    def __init__(self, *, cwd: Path = DESK_DIR, run=subprocess.run, executable: str = "claude", model: str | None = None):
        self.cwd, self.run, self.executable, self.model = cwd, run, executable, model

    def command(self, session: Session | None = None) -> list[str]:
        text = INSTRUCTIONS.read_text()  # read once: the hash we report is of exactly the text we send
        cmd = [self.executable, "-p", (session.intro + "\n\n" if session and session.intro else "") + PROMPT,
               "--output-format", "stream-json", "--verbose", "--json-schema", json.dumps(SCHEMA),  # stream-json lists every tool call; json does not
               "--tools", "Skill", "--system-prompt", text,
               "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
               "--setting-sources", "",  # no settings or CLAUDE.md files: the system prompt carries the constitution once
               "--plugin-dir", str(self.cwd)]  # the skills (.claude/skills) arrive as a plugin
        if session is None:
            cmd.append("--no-session-persistence")  # one question, no memory
        else:
            # Without "off", a resumed conversation silently keeps the system prompt of its first request, so the
            # hash we journal could differ from what the trader read. With "off" every request sends `text`.
            cmd += ["--system-prompt-snapshot", "off", "--resume" if session.resume else "--session-id", session.id]
            session.instructions = instructions_hash(text)
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
        out, skills = parse_stream(done.stdout)
        if out is None:
            raise ModelFailure("bad_output", f"no result event in stdout: {done.stdout[:200]!r}")
        if session is not None:
            session.skills = skills
        if session is not None:
            session.context = context_usage(out)  # even a bad answer cost context
            session.id = out.get("session_id") or session.id  # the id claude reports is the one to resume next time
        if out.get("is_error"):
            raise ModelFailure("error", str(out.get("result", out))[:300])
        if "structured_output" not in out:
            raise ModelFailure("bad_output", "no structured_output in the answer")
        return out["structured_output"]
