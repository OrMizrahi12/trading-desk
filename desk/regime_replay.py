"""Which saved bars would the regime gate have woken Claude for, and what would that have saved?

`uv run -m desk.regime_replay [--snapshots DIR] [--journal FILE] [--trace]`

Replays the full snapshots in `data/desk/snapshots` (real delayed paper data) through the same `regime.decide` the heartbeat
uses, bar by bar in time order, with no model and no broker. The costs and times of the bars it would have skipped come
from the journal (`context.bar_cost_usd` is claude's own list-price cost, `elapsed_s` the time of the whole bar), so the
savings are what those bars really cost, not an estimate. Nothing is tuned: the threshold is fixed in regime.py.

Bars of a closed market (the desk still polled after 16:00) are listed apart: no entry is possible on them either way.
Paper trading only.
"""

import argparse
import json
from collections import Counter
from pathlib import Path

from . import regime
from .model import DESK_DIR

SNAPSHOTS = DESK_DIR / "data" / "desk" / "snapshots"
JOURNAL = DESK_DIR / "data" / "desk" / "journal.jsonl"


def load_snapshots(directory: Path) -> list[tuple[str, dict]]:
    """(file stem, snapshot) for every full snapshot, oldest first. Delta files are what was sent, not the snapshot."""
    files = sorted(p for p in directory.glob("*.json") if not p.name.endswith(".delta.json"))
    return [(p.stem, json.loads(p.read_text())) for p in files]


def load_journal(path: Path) -> dict[str, dict]:
    """Journal records by the stem of their snapshot file."""
    out: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            rec = json.loads(line)
            if rec.get("snapshot_file"):
                out[Path(rec["snapshot_file"]).stem] = rec
    return out


def replay(snaps: list[tuple[str, dict]], journal: dict[str, dict]) -> dict:
    rows, fills_seen = [], None
    for stem, snap in snaps:
        v = regime.decide(snap, fills_seen=fills_seen)
        fills_seen = len(snap.get("todays_fills") or [])
        rec = journal.get(stem) or {}
        mtc = (snap.get("session") or {}).get("minutes_to_close")
        rows.append({"bar": stem, "wake": v.wake, "why": v.why, "regime": v.regime, "distance_atr": v.distance_atr,
                     "open_market": mtc is not None and mtc >= 0,
                     "action": (rec.get("decision") or {}).get("action"),
                     "cost_usd": (rec.get("context") or {}).get("bar_cost_usd"),
                     "elapsed_s": rec.get("elapsed_s"), "in_journal": bool(rec)})
    return {"rows": rows, "summary": summarize(rows)}


def summarize(rows: list[dict]) -> dict:
    def part(rs: list[dict]) -> dict:
        asleep = [r for r in rs if not r["wake"]]
        costed = [r for r in asleep if r["cost_usd"] is not None]
        return {"bars": len(rs), "woken": len(rs) - len(asleep), "skipped": len(asleep),
                "skipped_with_cost_on_record": len(costed),
                "saved_usd": round(sum(r["cost_usd"] for r in costed), 4),
                "saved_seconds": round(sum(r["elapsed_s"] or 0 for r in asleep), 1),
                "total_usd": round(sum(r["cost_usd"] or 0 for r in rs), 4),
                "skipped_but_trader_acted": [r["bar"] for r in asleep if r["action"] not in (None, "wait")],
                "wake_reasons": dict(Counter(r["why"] for r in rs if r["wake"])),
                "max_distance_atr": max((r["distance_atr"] for r in rs if r["distance_atr"] is not None), default=None)}

    return {"all": part(rows), "market_open": part([r for r in rows if r["open_market"]]),
            "market_closed": part([r for r in rows if not r["open_market"]]),
            "regimes": dict(Counter(r["regime"] for r in rows if r["open_market"]))}


def render(result: dict, trace: bool) -> str:
    s = result["summary"]
    out = [f"Regime gate replay: trending when |close - today's open| / ATR(14) > {regime.TREND_DISTANCE_ATR} "
           "(lab H1, not tuned).", ""]
    for key, title in (("market_open", "Market open (16:00 close not yet reached)"), ("market_closed", "Market closed"),
                       ("all", "All saved bars")):
        p = s[key]
        share = f"{100 * p['skipped'] / p['bars']:.0f}%" if p["bars"] else "n/a"
        out += [f"{title}: {p['bars']} bars, Claude woken on {p['woken']}, skipped {p['skipped']} ({share}).",
                f"  saved: ${p['saved_usd']:.2f} of ${p['total_usd']:.2f} spent on these bars, "
                f"{p['saved_seconds']:.0f} s of bar time ({p['skipped_with_cost_on_record']} of the skipped bars have a cost in the journal)",
                f"  woken because: {p['wake_reasons'] or 'none'}",
                f"  largest distance seen: {p['max_distance_atr']} ATR",
                f"  bars skipped where the real trader did something other than wait: "
                f"{p['skipped_but_trader_acted'] or 'none'}", ""]
    out.append(f"Regime of the market-open bars: {s['regimes']}")
    if trace:
        out += ["", "bar               wake  regime    dist  why"]
        out += [f"{r['bar']}  {'WAKE' if r['wake'] else '----'}  {r['regime']:<8}  {r['distance_atr'] if r['distance_atr'] is not None else '-':>5}  {r['why']}"
                for r in result["rows"]]
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshots", type=Path, default=SNAPSHOTS)
    ap.add_argument("--journal", type=Path, default=JOURNAL)
    ap.add_argument("--trace", action="store_true", help="list every bar with its label and reason")
    args = ap.parse_args()
    print(render(replay(load_snapshots(args.snapshots), load_journal(args.journal)), args.trace))


if __name__ == "__main__":
    main()
