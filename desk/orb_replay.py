"""Replay the ORB veto on the saved snapshots: every signal, and what Claude said about it.

`uv run -m desk.orb_replay [--ask] [--model M] [--snapshots DIR]`

Two views of the same days, because the feed revises bars (a bar's close in the snapshot taken when it had just closed can
differ from the same bar in later snapshots):

- as seen: the lab strategy runs on each snapshot exactly as the desk saved it. This is what `--orb-veto` would have done live.
- revised: the newest version of every bar is stitched into one series and the strategy runs on each prefix. This is the
  signal the day's final data contains, and it may not have existed at the time.

`--ask` sends each signal to the real veto (`claude -p`, no tools): it spends a few cents of API money and touches no account.
For a revised-only signal the snapshot of that bar is sent with its 5-minute bars replaced by the revised ones and a note
saying so. Without `--ask` the signals are only listed. Paper trading only.
"""

import argparse
import json
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from . import lab  # noqa: F401
from strategies.opening_range_breakout import OpeningRangeBreakout

from .orb_veto import SIZE_R, Signal, Vetoer, bars_frame, signal
from .regime_replay import SNAPSHOTS, load_snapshots

NY = "America/New_York"
ROWS = 100  # bars a snapshot carries


def as_seen(snaps: list[tuple[str, dict]]) -> list[dict]:
    """Signals on each snapshot exactly as saved."""
    out = []
    for stem, snap in snaps:
        sig = signal(snap)
        if sig:
            out.append({"bar": stem, "signal": sig, "snapshot": snap, "revised": False})
    return out


def revised_series(snaps: list[tuple[str, dict]]) -> pd.DataFrame:
    """One series: for each bar the version from the NEWEST snapshot that has it."""
    best: dict[pd.Timestamp, dict] = {}
    for _, snap in snaps:
        for r in snap["bars_5min"]:
            best[pd.Timestamp(r["start"])] = r
    rows = [best[k] for k in sorted(best)]
    return bars_frame({"bars_5min": rows}), rows


def revised(snaps: list[tuple[str, dict]]) -> list[dict]:
    """Signals when the strategy runs on every prefix of the revised series. The context is the snapshot of that bar with the
    revised bars swapped in (and a note), or None when no snapshot was saved for it."""
    frame, rows = revised_series(snaps)
    by_bar = {pd.Timestamp(s["session"]["bar_start"]): (stem, s) for stem, s in snaps}
    strategy, out = OpeningRangeBreakout(), []
    for i in range(len(frame)):
        sig = signal({"bars_5min": rows[max(0, i + 1 - ROWS): i + 1]}, strategy)
        if not sig:
            continue
        stem, snap = by_bar.get(frame.index[i].tz_convert(NY), (None, None))
        ctx = None if snap is None else {**snap, "bars_5min": rows[max(0, i + 1 - ROWS): i + 1],
                                         "replay_note": "bars_5min shows the latest revision of each bar, not what the "
                                                        "feed showed when this bar had just closed"}
        out.append({"bar": stem or frame.index[i].isoformat(), "signal": sig, "snapshot": ctx, "revised": True})
    return out


def revisions(snaps: list[tuple[str, dict]]) -> dict:
    """How many bars changed between the first snapshot that showed them and the newest one."""
    first, last = {}, {}
    for _, snap in snaps:
        for r in snap["bars_5min"]:
            k = pd.Timestamp(r["start"])
            first.setdefault(k, r["close"])
            last[k] = r["close"]
    changed = [k for k in first if first[k] != last[k]]
    return {"bars": len(first), "changed": len(changed)}


def run(snaps: list[tuple[str, dict]], vetoer: Vetoer | None) -> dict:
    seen, rev = as_seen(snaps), revised(snaps)
    for item in seen + rev:
        item["view"] = (vetoer.ask(item["snapshot"], item["signal"], 120.0)
                        if vetoer and item["snapshot"] is not None else None)
    return {"as_seen": seen, "revised": rev, "revisions": revisions(snaps), "snapshots": len(snaps)}


def render(result: dict) -> str:
    out = [f"ORB veto replay over {result['snapshots']} saved snapshots (lab opening_range_breakout, default settings, "
           f"fixed size {SIZE_R} R, stop = opening range low, no target).",
           f"Revised bars: {result['revisions']['changed']} of {result['revisions']['bars']} bars have a different close in "
           "the newest snapshot than in the first one that showed them.", ""]
    for key, title in (("as_seen", "Signals on the snapshots as the desk saved them (what --orb-veto would have seen)"),
                       ("revised", "Signals on the newest version of every bar (may not have existed at the time)")):
        items = result[key]
        out.append(f"{title}: {len(items)}")
        for it in items:
            sg: Signal = it["signal"]
            out.append(f"  bar {it['bar']}: buy at about {sg.entry:.2f} after a close above the range high {sg.range_high:.2f}, "
                       f"stop {sg.stop:.2f} (risk {sg.entry - sg.stop:.2f} a share), no target, size {SIZE_R} R")
            v = it.get("view")
            if v is None:
                out.append("    Claude: not asked" + (" (no snapshot saved for this bar)" if it["snapshot"] is None else ""))
            elif v["answer"] is None:
                out.append(f"    Claude: no valid answer ({v['failure']}) -> the desk would wait")
            else:
                a = v["answer"]
                out.append(f"    Claude: {a['verdict'].upper()}: {a['reason']}  (${v['cost_usd'] or 0:.3f}, {v['elapsed_s']} s)")
        out.append("")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshots", type=Path, default=SNAPSHOTS)
    ap.add_argument("--ask", action="store_true", help="really ask Claude about each signal (spends API money)")
    ap.add_argument("--model", default=None)
    ap.add_argument("--json", type=Path, default=None, help="also write the result here")
    args = ap.parse_args()
    result = run(load_snapshots(args.snapshots), Vetoer(model=args.model) if args.ask else None)
    print(render(result))
    if args.json:
        args.json.write_text(json.dumps(result, indent=1, default=lambda o: asdict(o) if hasattr(o, "__dataclass_fields__") else str(o)))


if __name__ == "__main__":
    main()
