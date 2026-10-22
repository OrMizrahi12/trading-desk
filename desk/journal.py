"""The desk's own record: one JSON line per bar, plus the small state file that makes restarts safe."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DESK_DIR = Path(__file__).parent.parent / "data" / "desk"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


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

    def write_snapshot(self, bar_name: str, snapshot: dict) -> Path:
        path = self.root / "snapshots" / f"{bar_name}.json"
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
        return state

    def save(self) -> None:
        _atomic_write(self.path, json.dumps({"last_bar_ts": self.last_bar_ts, "sent_refs": self.sent_refs,
                                             "halted": self.halted, "entry_day": self.entry_day}, indent=1, sort_keys=True))
