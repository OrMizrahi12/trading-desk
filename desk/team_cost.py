"""What the team costs per bar compared with the single trader: `uv run -m desk.team_cost [--bars 6] [--stress-bars 3]`.

Replays saved snapshots (`data/desk/snapshots`, real delayed paper data) through the real `claude -p`, no broker and no
orders, as one day-session each: the first bar sends the full snapshot, later bars a delta, exactly like the heartbeat.
Three runs over the same consecutive bars:

- single:  the trader alone (what the desk runs today);
- team:    analyst, then the trader. The analyst almost always proposes `wait`, so the other two are not asked;
- stress:  the same, but the analyst's answer is replaced by a made-up buy proposal AFTER it was really asked, so the
           risk manager and the devil's advocate are asked too. This prices the most expensive bar the team can have;
           it says nothing about what the trader would do. Cost and time are real, the proposal is not.

Cost is claude's own `total_cost_usd` (list price, cache included). Writes `experiments/team-cost.md` and `.json`.
Spends real API money (a few dollars with the defaults) but never touches an account. Paper trading only.
"""

import argparse
import json
import statistics
import time
import uuid
from pathlib import Path

from . import snapshot
from .model import DESK_DIR, ClaudeModel, Session
from .team import ANALYST, Team, TeamModel

SNAPSHOTS = DESK_DIR / "data" / "desk" / "snapshots"
REPORT = DESK_DIR / "experiments" / "team-cost"
MODEL_TIMEOUT_S = 120.0  # no budget pressure while measuring; the elapsed time is what is reported


class StressTeam(Team):
    """Asks the analyst for real (its cost counts), then swaps its answer for a buy so the judges are asked too."""

    def ask(self, name, snap, timeout_s, proposal=None):
        view = super().ask(name, snap, timeout_s, proposal)
        if name == ANALYST:
            close = snap["bars_5min"][-1]["close"]
            view["answer"] = {"proposal": "buy", "size_r": 0.5, "stop": round(close * 0.998, 2),
                              "target": round(close * 1.004, 2), "reason": "MADE UP for the cost test", "made_up": True}
            view["failure"] = None
        return view


def load(start: str | None, bars: int, folder: Path = SNAPSHOTS) -> list[tuple[str, dict]]:
    """`bars` consecutive full snapshots (delta files skipped) starting at the file named `start` (default: the last ones)."""
    files = sorted(p for p in folder.glob("*.json") if "delta" not in p.name)
    first = next((i for i, p in enumerate(files) if p.name.startswith(start)), None) if start else max(len(files) - bars, 0)
    if first is None or first + bars > len(files):
        raise SystemExit(f"cannot take {bars} snapshots from {start or 'the end'}: {len(files)} saved")
    snaps = [(p.stem, json.loads(p.read_text())) for p in files[first:first + bars]]
    for _, snap in snaps:  # snapshots saved before the review gate existed lack its key; "open" is what the heartbeat sends then
        snap.setdefault("entry_review_gate", {"entries_open": True, "why_closed": None})
    return snaps


def run(label: str, model, snaps: list[tuple[str, dict]]) -> list[dict]:
    """One day-session over `snaps`. One row per bar: seconds, cost split into trader and team, and what was decided."""
    sid, prev, rows = str(uuid.uuid4()), None, []
    for i, (name, snap) in enumerate(snaps):
        session = Session(sid, resume=i > 0, intro="Replay for a cost test. Today is a trading day (New York time)." if i == 0 else "")
        session.full = snap
        sent = (snapshot.delta(snap, prev) if prev else None) or snap
        start = time.monotonic()
        try:
            answer, failure = model.decide(sent, MODEL_TIMEOUT_S, session), None
        except Exception as exc:  # a failed bar is a row too
            answer, failure = {"action": "FAILED"}, str(exc)
        wall = time.monotonic() - start
        sid, prev = session.id, snap["bars_5min"][-1]["start"]
        total = (session.context or {}).get("session_cost_usd") or 0.0
        before = rows[-1]["_running"] if rows else 0.0
        team = session.team or {}
        rows.append({"mode": label, "bar": name, "first": i == 0, "input": sent.get("snapshot_kind", "full"),
                     "wall_s": round(wall, 2), "trader_usd": round(total - before, 4), "team_usd": team.get("cost_usd") or 0.0,
                     "team_tokens": team.get("tokens") or 0, "trader_tokens": (session.context or {}).get("tokens"),
                     "roles": sorted(team.get("views", {})), "proposal": ((team.get("views", {}).get(ANALYST) or {}).get("answer") or {}).get("proposal"),
                     "tally": (team.get("tally") or {}).get("why"), "action": answer.get("action"), "failure": failure,
                     "_running": total})
        rows[-1]["bar_usd"] = round(rows[-1]["trader_usd"] + rows[-1]["team_usd"], 4)
        print(f"{label:6} {name} {rows[-1]['input']:5} {wall:6.1f}s  trader ${rows[-1]['trader_usd']:.4f}  team ${rows[-1]['team_usd']:.4f}"
              f"  -> {answer.get('action')}", flush=True)
    return rows


def summary(rows: list[dict]) -> dict:
    """Steady state = every bar after the session's first (a new session sends the whole snapshot and writes the cache)."""
    later = [r for r in rows if not r["first"]]
    mean = lambda key, rs: round(statistics.mean(r[key] for r in rs), 4) if rs else None  # noqa: E731
    return {"bars": len(rows), "first_bar_usd": rows[0]["bar_usd"], "first_bar_s": rows[0]["wall_s"],
            "steady_bars": len(later), "usd_per_bar": mean("bar_usd", later), "trader_usd": mean("trader_usd", later),
            "team_usd": mean("team_usd", later), "s_per_bar": mean("wall_s", later),
            "max_s": max(r["wall_s"] for r in rows), "failures": sum(1 for r in rows if r["failure"])}


def report(snaps, results: dict[str, list[dict]]) -> str:
    s = {k: summary(v) for k, v in results.items()}
    single, team = s["single"], s["team"]
    ratio = lambda a, b: f"{a / b:.1f}x" if a and b else "n/a"  # noqa: E731
    lines = ["# What the team costs per bar", "",
             "Paper trading only: saved snapshots of SPY (delayed, simulated account) replayed through the real `claude -p`; no orders.",
             "Terms: a **bar** is the open, high, low, close and volume of one 5-minute slice; **cost** is claude's own list-price",
             "total for the bar (cache writes and reads included); the **first bar** of a session sends the whole snapshot and is",
             "shown apart because every later bar sends only what changed (a **delta**).", "",
             f"Bars replayed: {snaps[0][0]} to {snaps[-1][0]} ({len(snaps)} bars). Reproduce: `uv run -m desk.team_cost`.", "",
             "| Run | Later bars | $ per bar | of which trader | of which team | Seconds per bar | Slowest bar (s) | First bar $ | Failed |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    names = {"single": "Single trader", "team": "Team, quiet bar (analyst proposes wait)",
             "stress": "Team, full path (made-up buy proposal, all three asked)"}
    for k, v in s.items():
        lines.append(f"| {names[k]} | {v['steady_bars']} | {v['usd_per_bar']} | {v['trader_usd']} | {v['team_usd']} | {v['s_per_bar']} | "
                     f"{v['max_s']} | {v['first_bar_usd']} | {v['failures']} |")
    lines += ["", f"- Quiet bar: the team costs **{ratio(team['usd_per_bar'], single['usd_per_bar'])}** the single trader in dollars and "
                  f"adds {round((team['s_per_bar'] or 0) - (single['s_per_bar'] or 0), 1)} s per bar."]
    if "stress" in s:
        lines.append(f"- Full path: **{ratio(s['stress']['usd_per_bar'], single['usd_per_bar'])}** the single trader and "
                     f"{s['stress']['s_per_bar']} s per bar. The heartbeat gives the model {75} s and the whole bar {120} s.")
    lines += ["", "## Every bar", "", "| Run | Bar | Input | Seconds | Trader $ | Team $ | Roles asked | Analyst | Count | Action |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for k, rows in results.items():
        for r in rows:
            lines.append(f"| {k} | {r['bar']} | {r['input']} | {r['wall_s']} | {r['trader_usd']} | {r['team_usd']} | "
                         f"{', '.join(r['roles']) or '-'} | {r['proposal'] or '-'} | {r['tally'] or '-'} | {r['action']} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--bars", type=int, default=6, help="consecutive saved bars to replay")
    parser.add_argument("--stress-bars", type=int, default=3, help="bars for the full-path run (0 = skip)")
    parser.add_argument("--start", default=None, help="file name prefix of the first snapshot (default: the newest bars)")
    parser.add_argument("--model", default=None)
    args = parser.parse_args()
    snaps = load(args.start, args.bars)
    results = {"single": run("single", ClaudeModel(model=args.model), snaps),
               "team": run("team", TeamModel(ClaudeModel(model=args.model), Team(model=args.model)), snaps)}
    if args.stress_bars:
        results["stress"] = run("stress", TeamModel(ClaudeModel(model=args.model), StressTeam(model=args.model)),
                                snaps[:args.stress_bars])
    for rows in results.values():
        for r in rows:
            r.pop("_running", None)
    REPORT.with_suffix(".md").write_text(report(snaps, results))
    REPORT.with_suffix(".json").write_text(json.dumps({k: {"summary": summary(v), "rows": v} for k, v in results.items()}, indent=1))
    print(f"written {REPORT.with_suffix('.md')}")


if __name__ == "__main__":
    main()
