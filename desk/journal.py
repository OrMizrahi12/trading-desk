"""The desk's own record: one JSON line per bar, plus the small state file that makes restarts safe."""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DESK_DIR = Path(__file__).parent.parent / "data" / "desk"
NY = ZoneInfo("America/New_York")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def bar_day(bar_iso: str) -> str:
    """The New York calendar date of a bar's start time."""
    return datetime.fromisoformat(bar_iso).astimezone(NY).date().isoformat()


class Journal:
    """Append-only `journal.jsonl`: snapshot hash, raw model answer, validated decision, gate verdict, result, timings."""

    def __init__(self, root: Path = DESK_DIR):
        self.root = root
        self.path = root / "journal.jsonl"

    def append(self, record: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            f.write(json.dumps(record, sort_keys=True, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def tail(self, n: int = 10) -> list[dict]:
        """The last `n` records, shortened to what the model needs to remember its own recent decisions."""
        if not self.path.exists():
            return []
        lines = self.path.read_text().splitlines()[-n:]
        out = []
        for line in lines:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            out.append({k: r.get(k) for k in ("bar", "decision", "verdict", "result", "model_failure", "invalid") if r.get(k)})
        return out

    def bar_records(self) -> list[dict]:
        """Every per-bar record (not seatbelt notes), oldest first."""
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("bar"):
                out.append(r)
        return out

    def closed_trades(self) -> list[dict]:
        """The `trade_closed` records (see trades.py), oldest first, each with an `at` key: when the desk noticed the end."""
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and isinstance(r.get("trade_closed"), dict):
                out.append({**r["trade_closed"], "noticed_at": r.get("at")})
        return out

    def day_summary(self, day: str | None = None, before: str | None = None) -> str | None:
        """Plain facts about one New York trading day, from the bar records: `day`, or the latest day before `before`.

        None when the journal has no such day. Built by Python from what happened, with no opinion added.
        """
        by_day: dict[str, list[dict]] = {}
        for r in self.bar_records():
            by_day.setdefault(bar_day(r["bar"]), []).append(r)
        if day is None:
            days = [d for d in by_day if before is None or d < before]
            if not days:
                return None
            day = max(days)
        recs = by_day.get(day)
        if not recs:
            return None
        counts: dict[str, int] = {}
        for r in recs:
            a = (r.get("decision") or {}).get("action", "none")
            counts[a] = counts.get(a, 0) + 1
        refused = sum(1 for r in recs if (r.get("verdict") or {}).get("allowed") is False)
        failed = sum(1 for r in recs if r.get("model_failure") or r.get("invalid") or r.get("error"))
        head = (f"{day}: {len(recs)} bars handled; decisions: " + ", ".join(f"{n} {a}" for a, n in sorted(counts.items()))
                + f"; {refused} refused by the desk's rules; {failed} bars with no usable answer.")
        lines = []
        for r in recs:
            res = r.get("result") or {}
            if res.get("status") and res["status"] != "nothing_to_sell":
                t = datetime.fromisoformat(r["bar"]).astimezone(NY).strftime("%H:%M")
                d = r.get("decision") or {}
                lines.append(f"- bar {t} {d.get('action')} -> {res['status']}"
                             + (f" ({res['shares']} shares)" if res.get("shares") else "") + f": {d.get('reason', '')[:160]}")
            for b in r.get("seatbelts") or []:
                lines.append(f"- seatbelt {b['name']} acted: {str(b.get('detail', ''))[:120]}")
        last = recs[-1].get("decision") or {}
        return "\n".join([head, *lines[:10],  # a busy day is cut short, the closing line always stays
                          f"- last decision ({last.get('action')}): {last.get('reason', '')[:200]}"])

    def write_snapshot(self, bar_name: str, snapshot: dict, kind: str = "") -> Path:
        path = self.root / "snapshots" / f"{bar_name}{kind}.json"
        _atomic_write(path, json.dumps(snapshot, indent=1, sort_keys=True, default=str))
        return path


@dataclass
class State:
    """What survives a restart: the last bar already handled, entry refs already sent, and a halt reason if any."""

    path: Path
    last_bar_ts: str | None = None
    sent_refs: list[str] = field(default_factory=list)
    halted: str | None = None
    entry_day: str | None = None  # New York date of the desk's open buy, to catch a position carried overnight
    session_id: str | None = None  # today's Claude conversation (one per New York trading day)
    session_day: str | None = None
    session_instructions: str | None = None  # instructions hash when the session started; a change rolls it over
    session_bars: int = 0  # bars answered in it so far
    session_last_bar: str | None = None  # start of the last bar the session answered: later bars send what changed since
    session_cost_usd: float | None = None  # claude's running cost total for the session, to get each bar's own cost
    session_context_share: float | None = None  # how full its context window was after that bar
    open_trade: dict | None = None  # the desk's long while it is open (entry, stop, target, sales so far), to journal its end

    @classmethod
    def load(cls, root: Path = DESK_DIR) -> "State":
        path = root / "state.json"
        state = cls(path)
        if path.exists():
            data = json.loads(path.read_text())
            state.last_bar_ts = data.get("last_bar_ts")
            state.sent_refs = data.get("sent_refs", [])
            state.halted = data.get("halted")
            state.entry_day = data.get("entry_day")
            for k in ("session_id", "session_day", "session_instructions"):
                setattr(state, k, data.get(k))
            state.session_bars = data.get("session_bars", 0)
            state.session_last_bar = data.get("session_last_bar")
            state.session_cost_usd = data.get("session_cost_usd")
            state.session_context_share = data.get("session_context_share")
            state.open_trade = data.get("open_trade")
        return state

    def save(self) -> None:
        _atomic_write(self.path, json.dumps({"last_bar_ts": self.last_bar_ts, "sent_refs": self.sent_refs,
                                             "halted": self.halted, "entry_day": self.entry_day,
                                             "session_id": self.session_id, "session_day": self.session_day,
                                             "session_instructions": self.session_instructions,
                                             "session_bars": self.session_bars, "session_last_bar": self.session_last_bar,
                                             "session_cost_usd": self.session_cost_usd,
                                             "session_context_share": self.session_context_share,
                                             "open_trade": self.open_trade}, indent=1, sort_keys=True))
