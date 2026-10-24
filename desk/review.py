"""The 50-trade review: results broken down by setup, time of day and market regime, each number with its uncertainty.

`uv run -m desk.review` prints the review for every block of 50 closed trades the journal has reached and no note holds
yet; `--write` saves each as `vault/Reviews/Review after N trades.md` (once: an existing note is never overwritten) and
`--commit` also commits it. The desk's note writer does the same by itself once the trade notes of the block exist.
Everything is computed by code from the journal and the trade notes. No language model writes a word of it.
Paper trading only.

Terms:
- R: 1% of the account, the money lost if a trade's stop (a resting order that sells at the loss limit) is hit. A trade's
  result is counted in R: +2R = it made twice what it risked, -1R = it lost what it risked.
- setup: a repeatable pattern worth trading (Opening range breakout, VWAP reclaim), read from the trade note's `setup:`.
- time of day: the New York time of the bar the buy was decided on.
- market regime: the trend of the 15-minute bars when the buy was decided (see trades.regime): up, down or mixed.
- win rate: share of trades with a result above 0. Its 95% range is a Wilson interval (the usual range for a share).
- 95% interval of the average R: the range the true average probably lies in, found by bootstrapping (re-drawing the same
  trades with replacement 10,000 times). The same for "versus the other trades": the difference between this group's
  average R and the average R of all the other trades of the block.

Reading rules, fixed in advance: a group with fewer than 10 trades is "too few to say". Otherwise it is "clearly better" or
"clearly worse" than the rest only if the whole interval of the difference lies above or below 0. Many groups are looked at,
so chance alone flags about 1 in 20 of them; the note says how many that is.
"""

import argparse
import math
import random
import re
import sys
import zlib
from datetime import datetime
from pathlib import Path

from .journal import DESK_DIR, NY, Journal, _atomic_write
from .lessons import BOOTSTRAP_DRAWS, CONFIDENCE, SEED_TAG, Stats, git_commit, trade_setups
from .memory import VAULT

BLOCK = 50  # closed trades per review
MIN_GROUP = 10  # trades a group needs before it is judged
REVIEWS = "Reviews"
TIMES = (("morning (before 11:00)", 0), ("midday (11:00 to 13:00)", 11 * 60), ("afternoon (from 13:00)", 13 * 60))
DIMENSIONS = (("Setup", "setup"), ("Time of day", "time"), ("Market regime (15-minute trend at entry)", "regime"))


def time_bucket(entry_bar: str) -> str:
    t = datetime.fromisoformat(entry_bar).astimezone(NY)
    minutes = t.hour * 60 + t.minute
    return [name for name, start in TIMES if minutes >= start][-1]


def wilson(wins: int, n: int) -> tuple[float, float] | None:
    """95% range of a share (wins out of n)."""
    if n == 0:
        return None
    z, p = 1.96, wins / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def _bootstrap(rng: random.Random, a: list[float], b: list[float] | None = None) -> tuple[float, float]:
    draws = sorted(sum(rng.choices(a, k=len(a))) / len(a) - (sum(rng.choices(b, k=len(b))) / len(b) if b else 0.0)
                   for _ in range(BOOTSTRAP_DRAWS))
    tail = (1 - CONFIDENCE) / 2
    return draws[int(tail * BOOTSTRAP_DRAWS)], draws[int((1 - tail) * BOOTSTRAP_DRAWS) - 1]


def group_stats(label: str, dimension: str, rs: list[float], rest: list[float]) -> dict:
    rng = random.Random(zlib.crc32(f"{dimension}|{label}".encode()))
    s = Stats.of(rs)
    out = {"label": label, "n": s.n, "wins": sum(r > 0 for r in rs), "mean": s.mean, "sd": s.sd, "mean_interval": None,
           "diff": None, "diff_interval": None, "reading": ""}
    out["win_range"] = wilson(out["wins"], s.n)
    if s.n >= 2:
        out["mean_interval"] = _bootstrap(rng, rs)
    if dimension == "all":
        out["reading"] = "the baseline the groups below are compared with"
    elif s.n < MIN_GROUP:
        out["reading"] = f"too few to say ({s.n} trades, {MIN_GROUP} needed)"
    elif len(rest) < MIN_GROUP:
        out["reading"] = "too few other trades to compare with"
    else:
        out["diff"] = s.mean - sum(rest) / len(rest)
        out["diff_interval"] = lo_hi = _bootstrap(rng, rs, rest)
        out["reading"] = "clearly better than the rest" if lo_hi[0] > 0 else "clearly worse than the rest" if lo_hi[1] < 0 \
            else "no clear difference"
    return out


def breakdown(trades: list[dict], setups: dict[str, str]) -> dict:
    """{dimension title: [group dicts]} for the trades that have a result, plus the 'all' row and counts."""
    key = lambda t: t.get("ref") or t["entry_bar"]
    scored = [t for t in trades if t.get("result_r") is not None]
    labels = {"setup": lambda t: setups.get(key(t), "unknown"), "time": lambda t: time_bucket(t["entry_bar"]),
              "regime": lambda t: t.get("regime") or "not recorded"}
    out: dict = {"all": group_stats("all trades", "all", [t["result_r"] for t in scored], []),
                 "scored": len(scored), "unscored": len(trades) - len(scored), "dimensions": {}}
    for title, dim in DIMENSIONS:
        names = sorted({labels[dim](t) for t in scored}, key=lambda n: [x[0] for x in TIMES].index(n) if dim == "time" else n)
        groups = []
        for name in names:
            inside = [t["result_r"] for t in scored if labels[dim](t) == name]
            rest = [t["result_r"] for t in scored if labels[dim](t) != name]
            groups.append(group_stats(name, dim, inside, rest))
        out["dimensions"][title] = groups
    return out


def _r(x) -> str:
    return "n/a" if x is None else f"{x:+.2f}R"


def _span(iv, fmt=_r) -> str:
    return "n/a" if iv is None else f"{fmt(iv[0])} to {fmt(iv[1])}"


def _pct(x) -> str:
    return f"{100 * x:.0f}%"


def row(g: dict) -> str:
    win = "n/a" if not g["n"] else f"{_pct(g['wins'] / g['n'])} ({_span(g['win_range'], _pct)})"
    return (f"| {g['label']} | {g['n']} | {win} | {_r(g['mean'])} | {_span(g['mean_interval'])} | "
            f"{_r(g['diff'])} ({_span(g['diff_interval'])}) | {g['reading']} |" if g["diff"] is not None else
            f"| {g['label']} | {g['n']} | {win} | {_r(g['mean'])} | {_span(g['mean_interval'])} | n/a | {g['reading']} |")


HEADER = ("| Group | Trades | Win rate (95% range) | Average R | 95% interval of the average | Versus the other trades "
          "(difference, 95% interval) | Reading |\n|---|---|---|---|---|---|---|")


def render(n: int, trades: list[dict], result: dict, label: str) -> str:
    days = sorted(t["day"] for t in trades)
    judged = [g for groups in result["dimensions"].values() for g in groups if g["diff"] is not None]
    small = sum(g["n"] < MIN_GROUP for groups in result["dimensions"].values() for g in groups)
    flagged = [(t, g) for t, groups in result["dimensions"].items() for g in groups if g["reading"].startswith("clearly")]
    lines = ["---", "type: review", f"trades: {n}", f"first_day: {days[0]}", f"last_day: {days[-1]}",
             *(["seeded: true"] if label else []), "---", f"# Review after {n} trades", ""]
    if label:
        lines += [f"> [!danger] {label}", "> Every trade below is made up. Nothing here says anything about the real desk.", ""]
    lines += [f"The first {n} closed trades of the desk, {days[0]} to {days[-1]}, split three ways. **R** = 1% of the account, "
              "lost if a trade's stop (a resting order that sells at the loss limit) is hit; a result of +2R means the trade "
              "made twice what it risked. **Average R** = the mean result. The **95% interval** is the range the true value "
              "probably lies in, found by re-drawing the same trades 10,000 times (bootstrap); a wide interval means the "
              "number is not known well. **Versus the other trades** = this group's average R minus the average R of all "
              "other trades in the block. All numbers are computed by `desk/review.py` from the journal and the trade notes.",
              "", f"{result['scored']} trades have a result in R; {result['unscored']} have none recorded and are left out.",
              "", "## All trades", HEADER.replace("Versus the other trades (difference, 95% interval)", "Versus the other trades"),
              row(result["all"]), ""]
    for title, groups in result["dimensions"].items():
        lines += [f"## {title}", HEADER, *(row(g) for g in groups), ""]
    lines += ["## What the numbers can and cannot say",
              f"- A group needs {MIN_GROUP} trades before it is judged; below that the average is mostly luck. "
              f"{small} of the {sum(len(g) for g in result['dimensions'].values())} groups here are below that.",
              f"- {len(judged)} groups were judged. Chance alone flags about 1 in 20 groups as clearly better or worse, "
              f"so about {len(judged) / 20:.1f} of them would be flagged even if nothing mattered."]
    if flagged:
        lines.append(f"- Flagged in this block: {len(flagged)}. " + "; ".join(f"{g['label']} ({t.split(' (')[0].lower()}): "
                                                                          f"{g['reading']}" for t, g in flagged)
                     + ". Treat each as a question for the next 50 trades, not as an answer.")
    else:
        lines.append("- Flagged in this block: none. No group is clearly different from the rest.")
    lines += ["- The three splits overlap (each trade is in one group of each split), so they are not independent evidence.",
              "- One block of trades is a sample; a setup, hour or regime is a rule only when later blocks agree.",
              "- Regime is the 15-minute trend at entry, worked out by the desk's code; trades from before it was recorded say "
              "`not recorded`. Setup comes from the trade note; a trade without a note says `unknown`.", ""]
    return "\n".join(lines)


def written_blocks(vault: Path) -> set[int]:
    return {int(m.group(1)) for p in (vault / REVIEWS).glob("Review after * trades.md") if (m := re.fullmatch(r"Review after (\d+) trades", p.stem))}


def due_blocks(trades: list[dict], vault: Path) -> list[int]:
    """Block sizes (50, 100, ...) the journal has reached and no note holds yet."""
    return [n for n in range(BLOCK, len(trades) + 1, BLOCK) if n not in written_blocks(vault)]


def write_due(journal: Journal, vault: Path, *, label_for=None, commit: bool = False, only_with_notes: bool = True) -> list[Path]:
    """Write each due review. With `only_with_notes` a block waits until every trade in it has its trade note (that is
    where the setup comes from), so the review is never written with a setup missing for a reason that will pass."""
    from .scribe import note_name
    all_trades = journal.closed_trades()
    written = []
    for n in due_blocks(all_trades, vault):
        block = all_trades[:n]
        seeded = {bool(t.get("seeded")) for t in block}
        if len(seeded) > 1:
            raise SystemExit("The block mixes seeded (made-up) and real trades. Refusing to review them together.")
        label = SEED_TAG if seeded == {True} else ""
        if label and vault.resolve() == VAULT.resolve():
            raise SystemExit("Seeded trades must never be written into the real vault.")
        if only_with_notes and not all((vault / "Trades" / f"{note_name(t)}.md").exists() for t in block):
            continue
        path = vault / REVIEWS / f"Review after {n} trades.md"
        _atomic_write(path, render(n, block, breakdown(block, trade_setups(vault, block)), label))
        if commit:
            git_commit([path], f"{label + ': ' if label else ''}Review after {n} trades")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--desk-dir", type=Path, default=DESK_DIR)
    parser.add_argument("--vault", type=Path, default=VAULT)
    parser.add_argument("--write", action="store_true", help="save the due reviews as notes (never overwrites)")
    parser.add_argument("--commit", action="store_true", help="save and commit them")
    args = parser.parse_args(argv)
    journal = Journal(args.desk_dir)
    trades = journal.closed_trades()
    if args.write or args.commit:
        paths = write_due(journal, args.vault, commit=args.commit)
        print("\n".join(f"wrote {p}" for p in paths) or f"No review is due ({len(trades)} closed trades; one every {BLOCK}).")
        return 0
    blocks = due_blocks(trades, args.vault)
    if not blocks:
        print(f"No review is due: {len(trades)} closed trades, one review every {BLOCK}; "
              f"reviews already written: {sorted(written_blocks(args.vault)) or 'none'}.")
        return 0
    for n in blocks:
        block = trades[:n]
        seeded = {bool(t.get("seeded")) for t in block}
        print(render(n, block, breakdown(block, trade_setups(args.vault, block)), SEED_TAG if seeded == {True} else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
