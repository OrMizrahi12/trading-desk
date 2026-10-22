"""Search over the two append-only journals: the heartbeat's (`data/desk`) and this server's own (`data/mcp`).

Pure file reading, no IB. Records are written by two different writers, so every field is read defensively: a line
that is not JSON is skipped, and a seatbelt-only record has no `bar`.
"""

import json
from pathlib import Path

import pandas as pd

from ..journal import DESK_DIR
from .core import MCP_DIR, Refused

MAX_LIMIT = 200
NY = "America/New_York"


def _when(rec: dict) -> str | None:
    return rec.get("bar") or rec.get("at") or rec.get("seen_at")


def _compact(source: str, rec: dict) -> dict:
    """The few fields that answer 'what did we decide, why, and what happened'."""
    decision, verdict, result = rec.get("decision") or {}, rec.get("verdict") or {}, rec.get("result")
    request = rec.get("request") or {}
    out = {"source": source, "when": _when(rec)}
    if rec.get("tool"):
        out.update(tool=rec["tool"], outcome=rec.get("outcome"), reason=rec.get("reason") or request.get("reason"),
                   request=request)
    else:
        out.update(action=decision.get("action"), size_r=decision.get("size_r"), stop=decision.get("stop"),
                   target=decision.get("target"), reason=decision.get("reason"))
    if verdict:
        out["verdict"] = {k: verdict.get(k) for k in ("allowed", "reason", "shares", "seatbelt", "note") if verdict.get(k) not in (None, "")}
    if result is not None:
        out["result"] = result
    for key in ("seatbelt", "seatbelts", "invalid", "model_failure", "error", "halted"):
        if rec.get(key):
            out[key] = rec[key]
    return {k: v for k, v in out.items() if v not in (None, {}, [])}


def _parse(text) -> pd.Timestamp | None:
    try:
        ts = pd.Timestamp(text)
    except (ValueError, TypeError):
        return None
    if ts is pd.NaT:
        return None
    return ts.tz_localize(NY) if ts.tzinfo is None else ts  # a bare date or time means New York time


def _stamp(text: str | None) -> pd.Timestamp | None:
    if not text:
        return None
    ts = _parse(text)
    if ts is None:
        raise Refused(f"{text!r} is not a date/time; use ISO like 2026-10-22 or 2026-10-22T10:30:00-04:00")
    return ts


def search(*, text: str | None = None, action: str | None = None, tool: str | None = None, seatbelt: str | None = None,
           status: str | None = None, since: str | None = None, until: str | None = None, source: str = "all",
           limit: int = 20, desk_dir: Path = DESK_DIR, mcp_dir: Path = MCP_DIR) -> dict:
    if source not in ("all", "desk", "mcp"):
        raise Refused("source must be 'all', 'desk' (the heartbeat) or 'mcp' (this server's trades)")
    lo, hi = _stamp(since), _stamp(until)
    needle = text.lower() if text else None
    limit = max(1, min(int(limit), MAX_LIMIT))
    found, scanned = [], 0
    for name, folder in (("desk", desk_dir), ("mcp", mcp_dir)):
        path = Path(folder) / "journal.jsonl"
        if source not in ("all", name) or not path.exists():
            continue
        for line in path.read_text().splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            scanned += 1
            when = _parse(_when(rec))
            if lo or hi:
                if when is None:
                    continue  # no usable time: cannot satisfy a date filter
                if (lo and when < lo) or (hi and when > hi):
                    continue
            compact = _compact(name, rec)
            if action and compact.get("action") != action:
                continue
            if tool and compact.get("tool") != tool:
                continue
            if seatbelt and seatbelt not in json.dumps([rec.get("seatbelt"), rec.get("seatbelts"), (rec.get("verdict") or {}).get("seatbelt")]):
                continue
            if status and (compact.get("result") or {}).get("status") != status and compact.get("outcome") != status:
                continue
            if needle and needle not in json.dumps(rec, default=str).lower():
                continue
            found.append((when or pd.Timestamp("1970-01-01", tz="UTC"), len(found), compact))
    found.sort(key=lambda x: (x[0], x[1]), reverse=True)  # newest first (ties: later line first)
    return {"matches": len(found), "returned": min(limit, len(found)), "records_scanned": scanned,
            "records": [c for _, _, c in found[:limit]]}
