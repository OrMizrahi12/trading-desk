"""The opposite design: the lab's opening range breakout proposes, Claude may only veto. `--orb-veto`.

The opening range is the high and low of the first 5 minutes after the 09:30 open. The lab's `OpeningRangeBreakout`
strategy (strategies/opening_range_breakout.py, default settings, imported and not changed) buys when a 5-minute bar closes
above that high: stop = the range low, no profit target, at most one signal a day, so a vetoed signal is not offered again.

The desk (this file, not a model) runs the protocol on every bar:

1. Holding a long or an order is open: `wait`. The exits are the stop and the desk's flatten before the close; Claude is
   not asked and cannot close or sell (it creates nothing). The strategy's own time exit is that same flatten.
2. No ORB signal on the bar: `wait`. Claude is not asked.
3. A signal: Claude gets the bar's FULL snapshot plus the signal and answers `approve` or `veto` with a reason.
   `approve` becomes a `buy` with the strategy's stop, no target and the fixed size SIZE_R. Anything else (a veto, no
   answer, a late, invalid or malformed answer) becomes `wait`: with no clear yes there is no trade.

Claude never sets or changes the size, stop or target, and every desk gate (first 15 minutes, stale data, loss limits, the
STOP file, risk limits) still applies afterwards. Paper trading only: this file asks a question and counts the answer.
"""

import hashlib
import json
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

from . import lab  # noqa: F401
from strategies.base import Entry
from strategies.opening_range_breakout import OpeningRangeBreakout

from .decision import MAX_REASON
from .model import DESK_DIR, ModelFailure, Session

ROLE = DESK_DIR / ".claude" / "orb" / "veto.md"
SIZE_R = 0.5  # fixed by the desk (CLAUDE.md: "typically 0.5"); Claude cannot change it
VETO_TIMEOUT_S = 45.0
PROMPT = "Approve or veto the opening range breakout signal for the closed bar in the JSON snapshot on stdin."
VERDICTS = ("approve", "veto")
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["verdict", "reason"],
          "properties": {"verdict": {"type": "string", "enum": list(VERDICTS)},
                         "reason": {"type": "string", "maxLength": MAX_REASON,
                                    "description": "one plain sentence; explain any trading term; a concrete reason for a veto"}}}


@dataclass(frozen=True)
class Signal:
    entry: float  # the signal bar's close (the order fills at the next open)
    stop: float  # the opening range low
    range_high: float
    reason: str


def veto_prompt(path: Path = ROLE) -> str:
    return path.read_text().strip()


def prompt_hash(path: Path = ROLE) -> str:
    return hashlib.sha1(veto_prompt(path).encode()).hexdigest()[:12]


def bars_frame(snap: dict) -> pd.DataFrame:
    """The snapshot's 5-minute rows as the strategy wants them: UTC index, open/high/low/close/volume."""
    df = pd.DataFrame(snap.get("bars_5min") or [])
    if df.empty:
        return df
    df.index = pd.DatetimeIndex(pd.to_datetime(df["start"], utc=True))
    return df[["open", "high", "low", "close", "volume"]]


def signal(snap: dict, strategy: OpeningRangeBreakout | None = None) -> Signal | None:
    """The lab strategy's entry on the snapshot's newest bar, or None. The strategy decides alone; nothing is adjusted."""
    bars, strategy = bars_frame(snap), strategy or OpeningRangeBreakout()
    if bars.empty:
        return None
    out = strategy.on_bar(bars, None)
    if not isinstance(out, Entry):
        return None
    today = bars[bars.index.tz_convert("America/New_York").date == bars.index[-1].tz_convert("America/New_York").date()]
    open_range = today.iloc[: strategy.p.range_minutes // 5]
    return Signal(entry=float(bars["close"].iloc[-1]), stop=float(out.stop), range_high=float(open_range["high"].max()),
                  reason=out.reason)


class InvalidVerdict(ValueError):
    pass


def check_verdict(raw) -> dict:
    if not isinstance(raw, dict) or set(raw) != {"verdict", "reason"}:
        raise InvalidVerdict("keys must be exactly ['reason', 'verdict']")
    reason = raw["reason"]
    if raw["verdict"] not in VERDICTS:
        raise InvalidVerdict(f"verdict must be one of {VERDICTS}, got {raw['verdict']!r}")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_REASON:
        raise InvalidVerdict(f"reason must be a non-empty string of at most {MAX_REASON} characters")
    return {"verdict": raw["verdict"], "reason": reason.strip()}


class Vetoer:
    """Asks Claude (a fresh `claude -p`, no tools, no memory) to approve or veto. `run` is injectable for tests."""

    def __init__(self, *, cwd: Path = DESK_DIR, run=subprocess.run, executable: str = "claude", model: str | None = None,
                 role: Path = ROLE, mono=time.monotonic):
        self.cwd, self.run, self.executable, self.model, self.role, self.mono = cwd, run, executable, model, role, mono

    def command(self) -> list[str]:
        cmd = [self.executable, "-p", PROMPT, "--output-format", "json", "--json-schema", json.dumps(SCHEMA), "--tools", "",
               "--system-prompt", veto_prompt(self.role), "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
               "--setting-sources", "", "--no-session-persistence"]
        return cmd + (["--model", self.model] if self.model else [])

    def ask(self, snapshot: dict, sig: Signal, timeout_s: float) -> dict:
        """A record: `answer` (validated, or None), `failure` (why not), cost, seconds."""
        rec = {"answer": None, "failure": None, "cost_usd": None, "elapsed_s": 0.0}
        start = self.mono()
        try:
            if timeout_s <= 0:
                raise ModelFailure("timeout", "no time left")
            text = json.dumps({**snapshot, "orb_signal": asdict(sig)}, default=str)
            done = self.run(self.command(), input=text, capture_output=True, text=True, timeout=timeout_s, cwd=self.cwd)
            if done.returncode != 0:
                raise ModelFailure("exit_code", f"{done.returncode}: {done.stderr.strip()[:200]}")
            try:
                out = json.loads(done.stdout)
            except ValueError:
                raise ModelFailure("bad_output", f"not JSON: {done.stdout[:120]!r}") from None
            out = out[-1] if isinstance(out, list) and out else out
            if not isinstance(out, dict):
                raise ModelFailure("bad_output", "no result object")
            rec["cost_usd"] = out.get("total_cost_usd")
            if out.get("is_error"):
                raise ModelFailure("error", str(out.get("result", ""))[:200])
            if "structured_output" not in out:
                raise ModelFailure("bad_output", "no structured_output")
            rec["answer"] = check_verdict(out["structured_output"])
        except subprocess.TimeoutExpired:
            rec["failure"] = f"timeout: no answer within {timeout_s:.0f}s"
        except OSError as exc:
            rec["failure"] = f"not_started: {exc}"
        except ModelFailure as exc:
            rec["failure"] = str(exc)
        except InvalidVerdict as exc:
            rec["failure"] = f"invalid: {exc}"
        rec["elapsed_s"] = round(self.mono() - start, 2)
        return rec


def wait(reason: str) -> dict:
    return {"action": "wait", "size_r": 0, "stop": None, "target": None, "reason": reason[:MAX_REASON], "lessons": []}


def buy(sig: Signal, why: str) -> dict:
    return {"action": "buy", "size_r": SIZE_R, "stop": sig.stop, "target": None, "lessons": [],
            "reason": f"ORB signal, approved: {why}. No target: held to the stop or the flatten before the close."[:MAX_REASON]}


class OrbVetoModel:
    """A Model for the heartbeat: the lab strategy proposes, Claude may veto, the desk does the rest."""

    def __init__(self, vetoer: Vetoer | None = None, strategy: OpeningRangeBreakout | None = None):
        self.vetoer, self.strategy = vetoer or Vetoer(), strategy or OpeningRangeBreakout()

    def decide(self, snapshot: dict, timeout_s: float, session: Session | None = None) -> dict:
        full = session.full if session is not None and session.full else snapshot
        if session is not None:
            session.instructions = prompt_hash(self.vetoer.role)  # the text Claude is actually given, in the journal
        if full.get("positions") or full.get("open_orders"):
            return wait("ORB design: a position or order is open; the stop and the pre-close flatten manage it, Claude is not asked.")
        sig = signal(full, self.strategy)
        if sig is None:
            return wait("No opening range breakout signal on this bar.")
        record = {"signal": asdict(sig), "view": self.vetoer.ask(full, sig, min(VETO_TIMEOUT_S, timeout_s)), "final": None}
        if session is not None:
            session.orb = record
        view = record["view"]
        if view["answer"] is None:
            final = wait(f"ORB signal not taken: no valid answer from Claude ({view['failure']}). No clear yes is a wait.")
        elif view["answer"]["verdict"] == "veto":
            final = wait(f"ORB signal vetoed: {view['answer']['reason']}")
        else:
            final = buy(sig, view["answer"]["reason"])
        record["final"] = final["action"]
        return final
