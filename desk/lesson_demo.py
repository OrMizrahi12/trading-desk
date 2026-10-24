"""A SEEDED DEMO of the lesson tracker: made-up trades that show one promotion and one rollback. NOT REAL RESULTS.

`uv run -m desk.lesson_demo [--out DIR]` builds a small stand-alone desk in `DIR` (default `experiments/lesson-demo-SEEDED`):
a fake journal, a fake vault with two lessons, and nothing from the real desk. It commits that starting point to Git,
then runs `desk.lessons --commit` twice on it, two "dates" apart, so the Git history shows each change, and finally
writes the 50-trade review (`desk.review`) into the demo vault:
  1. as of 2026-09-04: lesson A has 20+ applications and does clearly better than similar trades -> PROMOTED;
  2. as of 2026-10-23: since its promotion, A has done clearly worse than similar trades -> ROLLED BACK.
  Lesson B looks good but never gets 20 applications: the tracker keeps it, which shows the 20-trade gate.

The results are drawn from a fixed random seed with the averages chosen below, so the outcome is by construction: the demo
shows how the machinery behaves, and says nothing about whether any lesson is true. Every file is labelled SEEDED, every
trade carries `seeded: true`, and the tracker refuses to apply seeded trades to the real vault.

R = 1% of the account, lost if a trade's stop (a resting order that sells at the loss limit) is hit.
"""

import argparse
import json
import random
import shutil
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from . import review
from .journal import NY, Journal
from .lessons import SEED_TAG, git_commit, run
from .scribe import note_name

SEED = 2026
FIRST_DAY, LAST_DAY = date(2026, 6, 1), date(2026, 10, 23)
PROMOTION_RUN, ROLLBACK_RUN = "2026-09-04", "2026-10-23"
SD = 0.9  # spread of a single trade's result, in R
LESSON_A = "SEED Wait for the retest after a breakout"
LESSON_B = "SEED Skip the first bar after a gap"
# (probability the trader applies it on a day it trades, average R when applied) per phase; trades that apply neither: 0.0R
PHASES = {"before_promotion": {LESSON_A: (0.55, +0.55), LESSON_B: (0.20, +0.40)},
          "after_promotion": {LESSON_A: (0.60, -0.70), LESSON_B: (0.20, +0.40)}}
BANNER = ("> [!danger] SEEDED DEMO, NOT A REAL LESSON\n"
          "> Made up to show the lesson tracker. No real trade taught this; the numbers are invented.\n")


def weekdays(first: date, last: date):
    d = first
    while d <= last:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


SETUPS = ("opening-range-breakout", "vwap-reclaim")
REGIMES = (("up", 0.4), ("down", 0.3), ("mixed", 0.3))


def make_trades() -> list[dict]:
    rng = random.Random(SEED)
    side = random.Random(SEED + 1)  # setup, entry time and regime come from their own stream, so the results above do not change
    trades = []
    for d in weekdays(FIRST_DAY, LAST_DAY):
        if rng.random() > 0.8:  # no entry on about one day in five
            continue
        phase = PHASES["before_promotion" if d.isoformat() <= PROMOTION_RUN else "after_promotion"]
        applied = [name for name, (p, _) in phase.items() if rng.random() < p]
        mean = sum(phase[name][1] for name in applied)
        r = round(rng.gauss(mean, SD), 2)
        minute = 9 * 60 + 45 + 5 * side.randrange(0, 63)  # a 5-minute bar from 09:45 to 14:55 New York time
        entry = datetime.combine(d, time(minute // 60, minute % 60), tzinfo=NY).astimezone(timezone.utc)
        setup = side.choice(SETUPS)
        regime = side.choices([n for n, _ in REGIMES], [w for _, w in REGIMES])[0]
        trades.append({"seeded": True, "ref": f"SEED-{d.isoformat()}", "entry_bar": entry.isoformat(), "day": d.isoformat(),
                       "setup": setup, "regime": regime, "result_r": r, "lessons": applied, "exit_type": "stop" if r <= -0.95 else "target" if r >= 1.95 else "close",
                       "entry_reason": "SEEDED, not a real trade"})
    return trades


def lesson_note(title: str, summary: str) -> str:
    return (f"---\ntype: lesson\nstatus: active\nseeded: true\nactivated: {FIRST_DAY.isoformat()}\nseen: 1\n"
            f"last_seen: {FIRST_DAY.isoformat()}\nsummary: {summary}\ntrades: none, seeded\n---\n# {title}\n\n{BANNER}\n"
            f"{summary}\n")


def build(out: Path) -> tuple[Path, Path]:
    """Write the seeded desk folder. Returns (desk dir with journal.jsonl, vault dir)."""
    if out.exists():
        shutil.rmtree(out)
    desk, vault = out / "desk", out / "vault"
    (vault / "Lessons" / "Archive").mkdir(parents=True)
    desk.mkdir(parents=True)
    (vault / "Trades").mkdir()
    with open(desk / "journal.jsonl", "w") as f:
        for t in make_trades():
            setup = t.pop("setup")  # the journal never holds a setup; the trade note does, as in the real desk
            (vault / "Trades" / f"{note_name(t)}.md").write_text(
                f"---\ntype: trade\nseeded: true\nsetup: {setup}\nresult_r: {t['result_r']}\n---\n# SEEDED trade {t['ref']}\n\n{BANNER}")
            f.write(json.dumps({"trade_closed": t, "at": t["entry_bar"], "seeded": True}, sort_keys=True) + "\n")
    (vault / "Lessons" / f"{LESSON_A}.md").write_text(lesson_note(
        LESSON_A, "After a bar closes above a level the price could not pass before, wait for the price to come back and hold that level before buying."))
    (vault / "Lessons" / f"{LESSON_B}.md").write_text(lesson_note(
        LESSON_B, "Do not buy on the first 5-minute bar after the market opens above the previous day's close (a gap)."))
    (out / "README.md").write_text(
        f"# {SEED_TAG}\n\nEverything in this folder is invented by `desk/lesson_demo.py` to show the lesson tracker "
        "(`desk/lessons.py`). The trades are random draws with averages chosen in advance, so the promotion and the rollback "
        "happen by construction. It says nothing about the desk or about any real lesson. Rebuild with "
        "`uv run -m desk.lesson_demo`.\n")
    return desk, vault


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=Path(__file__).parent.parent / "experiments" / "lesson-demo-SEEDED")
    args = parser.parse_args(argv)
    desk, vault = build(args.out)
    git_commit([args.out / "README.md", desk / "journal.jsonl", *vault.rglob("*.md")],
               f"{SEED_TAG}: starting point of the lesson tracker demo")
    for as_of in (PROMOTION_RUN, ROLLBACK_RUN):
        text, _ = run(desk, vault, as_of, commit=True)
        print(f"===== {SEED_TAG}: tracker run as of {as_of} =====\n{text}\n")
    for path in review.write_due(Journal(desk), vault, commit=True):
        print(f"===== {SEED_TAG}: wrote {path} =====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
