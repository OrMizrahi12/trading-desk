"""Lesson tracking: does a lesson the trader applied actually pay? Promote it, keep it, or archive it, and write it down.

Run `uv run -m desk.lessons` (a report only; nothing is changed). Add `--apply` to make the changes and `--commit` to also
record them in Git. Paper trading only; this reads the journal and edits notes in `vault/`, it never touches an account.

Terms:
- R: 1% of the account, the money lost if a trade's stop (a resting order that sells at the loss limit) is hit.
  A trade's result is counted in R: +2R = it made twice what it risked, -1R = it lost what it risked.
- average R: the mean result of a group of trades. SD (standard deviation): how widely the single results spread
  around that mean. Standard error: SD / sqrt(trades), how far the mean itself may be from the truth.
- 95% interval: the range the true difference probably lies in, computed by bootstrapping (re-drawing the same trades
  with replacement 10,000 times and looking at the middle 95% of the differences). A wide interval = too little data.

How a lesson is judged, lesson by lesson:
- "Applied" = the trader named the lesson in `lessons` on the decision that bought, or on a sell or close of that trade.
  "Similar, not applied" = trades that did not name it, made while the lesson was available (on or after its `activated`
  date, or the day after its `promoted` date), in a setup (a repeatable pattern, e.g. Opening range breakout) the applied trades also used.
  Trades with no recorded result, or from before the desk recorded lessons, are in neither group.
- PROMOTE (active -> promoted): at least 20 applied trades with a result, at least 10 similar ones, and the 95% interval of
  (average R applied - average R not applied) wholly above 0. A promoted lesson is listed in `Playbook/Promoted lessons.md`.
  It still loads on every bar (see memory.py).
- ARCHIVE (active -> archived): at least 10 and 10, and the interval wholly below 0.
- ROLLBACK (promoted -> archived): the same test, but only on trades after the promotion day. The promotion was chosen
  because the earlier trades looked good, so those trades cannot also confirm it.
- Otherwise KEEP: not enough evidence yet.
Many lessons tested at once will throw up some lucky "clearly better" ones by chance; that is why 20 trades and the full
interval are required, and why the change log keeps the numbers for a person to second-guess.
"""

import argparse
import json
import math
import random
import re
import subprocess
import sys
import zlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from .journal import DESK_DIR, NY, Journal, _atomic_write
from .memory import LESSONS, VAULT, split_front
from .scribe import set_front

PROMOTE_MIN_APPLIED = 20  # applied trades (with a result) before a lesson may enter the playbook
ARCHIVE_MIN_APPLIED = 10  # applied trades before the evidence may count against a lesson
MIN_CONTROL = 10  # similar trades without the lesson needed for any verdict
BOOTSTRAP_DRAWS = 10_000
CONFIDENCE = 0.95
TRACKED = ("active", "promoted")  # statuses the trader sees, so the only ones that can have applications
PLAYBOOK_NOTE = "Promoted lessons"
LOG_NOTE = "Rules change log"
LOG_DIR = "Changelog"
SEED_TAG = "DEMO (seeded, made-up data)"


def norm(name: str) -> str:
    return " ".join(str(name).split()).casefold()


# -- statistics ---------------------------------------------------------------------------------

@dataclass
class Stats:
    n: int
    mean: float | None
    sd: float | None
    se: float | None

    @classmethod
    def of(cls, xs: list[float]) -> "Stats":
        n = len(xs)
        if n == 0:
            return cls(0, None, None, None)
        mean = sum(xs) / n
        if n < 2:
            return cls(n, mean, None, None)
        sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / (n - 1))
        return cls(n, mean, sd, sd / math.sqrt(n))


def bootstrap_difference(a: list[float], b: list[float], seed: str) -> tuple[float, float] | None:
    """95% interval of mean(a) - mean(b). The seed comes from the lesson's name, so a re-run gives the same numbers."""
    if len(a) < 2 or len(b) < 2:
        return None
    rng = random.Random(zlib.crc32(seed.encode()))
    diffs = sorted(sum(rng.choices(a, k=len(a))) / len(a) - sum(rng.choices(b, k=len(b))) / len(b)
                   for _ in range(BOOTSTRAP_DRAWS))
    tail = (1 - CONFIDENCE) / 2
    return diffs[int(tail * BOOTSTRAP_DRAWS)], diffs[int((1 - tail) * BOOTSTRAP_DRAWS) - 1]


# -- reading the evidence -----------------------------------------------------------------------

@dataclass
class Lesson:
    title: str
    path: Path
    status: str
    front: dict
    summary: str = ""

    @property
    def since(self) -> str | None:
        """First day whose trades count: the day AFTER the promotion for a promoted lesson (the promotion day's own
        trades were part of the evidence that promoted it), else the day the lesson became active."""
        if self.status == "promoted":
            day = self.front.get("promoted")
            return (date.fromisoformat(day) + timedelta(days=1)).isoformat() if day else None
        return self.front.get("activated") or None


def read_lessons(vault: Path) -> list[Lesson]:
    out = []
    for path in sorted((vault / LESSONS).glob("*.md")):
        front, _ = split_front(path.read_text())
        if front.get("status") in TRACKED:
            out.append(Lesson(path.stem, path, front["status"], front, front.get("summary", "")))
    return out


def trade_setups(vault: Path, trades: list[dict]) -> dict[str, str]:
    """Setup of each trade from its trade note's front matter (`setup:`); 'unknown' when there is no note or no setup."""
    from .scribe import note_name
    out = {}
    for t in trades:
        path = vault / "Trades" / f"{note_name(t)}.md"
        setup = split_front(path.read_text())[0].get("setup", "") if path.exists() else ""
        out[t.get("ref") or t["entry_bar"]] = setup if setup and setup != "none" else "unknown"
    return out


@dataclass
class Verdict:
    lesson: Lesson
    action: str  # promote | archive | rollback | keep
    applied: Stats
    control: Stats
    interval: tuple[float, float] | None
    difference: float | None
    since: str | None
    since_note: str
    why: str
    unscored: int = 0  # applied trades whose result was not recorded
    setups: list[str] = field(default_factory=list)


def judge(lesson: Lesson, trades: list[dict], setups: dict[str, str], as_of: str) -> Verdict:
    """Compare the trades that applied `lesson` with similar ones that did not (see the module docstring)."""
    key = lambda t: t.get("ref") or t["entry_bar"]
    recorded = [t for t in trades if t["day"] <= as_of and isinstance(t.get("lessons"), list)]
    applying = [t for t in recorded if norm(lesson.title) in {norm(x) for x in t["lessons"]}]
    since, note = lesson.since, ""
    if since is None:
        since = min((t["day"] for t in applying), default=None)
        note = " (no date in the note; first application used, which favours the lesson: add `activated:`)"
    applying = [t for t in applying if since is not None and t["day"] >= since]
    scored = [t for t in applying if t.get("result_r") is not None]
    setup_set = sorted({setups[key(t)] for t in scored})
    names = {key(t) for t in applying}
    control = [t for t in recorded if key(t) not in names and since is not None and t["day"] >= since
               and t.get("result_r") is not None and setups[key(t)] in setup_set]
    a, c = [t["result_r"] for t in scored], [t["result_r"] for t in control]
    sa, sc = Stats.of(a), Stats.of(c)
    interval = bootstrap_difference(a, c, lesson.title)
    diff = None if sa.mean is None or sc.mean is None else sa.mean - sc.mean
    unscored = len(applying) - len(scored)

    action, why = "keep", ""
    promoted = lesson.status == "promoted"
    enough_control = sc.n >= MIN_CONTROL
    if interval is None or not enough_control:
        why = (f"only {sa.n} applied and {sc.n} similar trades with a result; a verdict needs a comparison group of "
               f"at least {MIN_CONTROL}")
    elif interval[1] < 0 and sa.n >= ARCHIVE_MIN_APPLIED:
        action = "rollback" if promoted else "archive"
        why = f"{sa.n} applied trades do clearly worse than similar ones (the whole 95% interval is below 0)"
    elif not promoted and sa.n >= PROMOTE_MIN_APPLIED and interval[0] > 0:
        action = "promote"
        why = f"{sa.n} applied trades do clearly better than similar ones (the whole 95% interval is above 0)"
    elif not promoted and sa.n < PROMOTE_MIN_APPLIED:
        why = f"{sa.n} of the {PROMOTE_MIN_APPLIED} applications needed before it can be promoted"
        if interval[0] > 0:
            why += " (it looks good so far, but that is not yet enough)"
    elif promoted:
        why = "no clear evidence against it since promotion" if sa.n >= ARCHIVE_MIN_APPLIED else \
            f"only {sa.n} applications since promotion; {ARCHIVE_MIN_APPLIED} are needed before it can be judged"
    else:
        why = "the 95% interval still includes 0: no clear difference"
    return Verdict(lesson, action, sa, sc, interval, diff, since, note, why, unscored, setup_set)


def evidence_line(v: Verdict) -> str:
    def part(s: Stats) -> str:
        return f"n={s.n}" + ("" if s.mean is None else f", average {s.mean:+.2f}R") + \
            ("" if s.sd is None else f", SD {s.sd:.2f}, standard error {s.se:.2f}")
    iv = "interval not computable" if v.interval is None else f"95% interval {v.interval[0]:+.2f}R to {v.interval[1]:+.2f}R"
    diff = "" if v.difference is None else f"difference {v.difference:+.2f}R, {iv}"
    return f"applied: {part(v.applied)}. Similar, not applied: {part(v.control)}. {diff}."


def render(verdicts: list[Verdict], *, trades: list[dict], unrecorded: int, unknown: dict[str, int], as_of: str,
           label: str) -> str:
    lines = [f"Lesson tracker, as of {as_of}" + (f"  [{label}]" if label else ""),
             "R = 1% of the account, lost if a trade's stop is hit. 'average R' is the mean result of a group of trades; "
             "SD = how widely single results spread; the 95% interval is the likely range of the true difference "
             "between the groups (bootstrap: the same trades re-drawn 10,000 times).",
             f"Closed trades in the journal: {len(trades)}; with a result in R: {sum(t.get('result_r') is not None for t in trades)}; "
             f"from before lessons were recorded (in no group): {unrecorded}.", ""]
    if not verdicts:
        lines.append("No lesson has status active or promoted, so there is nothing to compare.")
    for v in verdicts:
        lines += [f"{v.action.upper():<9} {v.lesson.title}  [{v.lesson.status}; counting trades from {v.since or 'n/a'}{v.since_note}]",
                  f"          {evidence_line(v)}",
                  *([f"          {v.unscored} applied trade(s) have no recorded result and are left out."] if v.unscored else []),
                  f"          -> {v.why}"]
    if unknown:
        lines += ["", "Lesson names the trader wrote that match no active or promoted lesson (typo, or a lesson since archived):",
                  *(f"  - {name!r} x{n}" for name, n in sorted(unknown.items()))]
    return "\n".join(lines)


# -- making the changes -------------------------------------------------------------------------

def _playbook_path(vault: Path) -> Path:
    return vault / "Playbook" / f"{PLAYBOOK_NOTE}.md"


def _log_path(vault: Path) -> Path:
    return vault / LOG_DIR / f"{LOG_NOTE}.md"


def _playbook_text(vault: Path, label: str) -> str:
    path = _playbook_path(vault)
    if path.exists():
        return path.read_text()
    banner = f"\n> [!danger] {label}\n> Nothing below is a real result.\n" if label else ""
    return ("---\ntype: playbook\nsetup: lessons\n---\n# Promoted lessons\n" + banner +
            "\nLessons that were applied in at least 20 trades and did clearly better than similar trades that did not "
            "apply them (rules and numbers: `desk/lessons.py`; history: [[Rules change log]]). A promoted lesson is a "
            "habit of the desk, never a loosening of [[Constitution risk rules]] or [[Lab risk limits]]: those win.\n")


def _remove_section(text: str, title: str) -> str:
    return re.sub(rf"\n## \[\[{re.escape(title)}\]\]\n.*?(?=\n## |\Z)", "", text, flags=re.S)


def _shown(path: Path) -> str:
    """A path as the repo knows it (relative to the repo root) when it lies inside the repo."""
    try:
        return path.resolve().relative_to(Path(__file__).parent.parent.resolve()).as_posix()
    except ValueError:
        return str(path)


def _append_log(vault: Path, day: str, v: Verdict, label: str, journal_path: str) -> Path:
    path = _log_path(vault)
    if path.exists():
        text = path.read_text()
    else:
        banner = f"\n> [!danger] {label}\n> Every entry below comes from made-up trades; none is a real result.\n" if label else ""
        text = ("---\ntype: changelog\n---\n# Rules change log\n" + banner +
                "\nEvery change `desk/lessons.py --apply` makes to which lessons the desk keeps: promotions into the "
                "playbook, archivings and rollbacks. Newest at the bottom. The same change is a Git commit; `git log "
                f"-- \"vault/{LOG_DIR}\"` lists them. Rules: the docstring of `desk/lessons.py`.\n")
    head = {"promote": "PROMOTED to the playbook", "archive": "ARCHIVED", "rollback": "ROLLED BACK (removed from the playbook, archived)"}[v.action]
    text = text.rstrip("\n") + (f"\n\n## {day} · {head} · [[{v.lesson.title}]]\n"
                                f"- Why: {v.why}.\n- Evidence: {evidence_line(v)}\n"
                                f"- Counted from: {v.since}. Source: {journal_path}.\n")
    if label:
        text += f"- {label}\n"
    _atomic_write(path, text)
    return path


def apply_change(v: Verdict, vault: Path, day: str, label: str, journal_path: str) -> list[Path]:
    """Carry out one verdict. Returns every path touched (including a path that no longer exists after a move)."""
    src, text = v.lesson.path, v.lesson.path.read_text()
    touched = [src]
    evidence = evidence_line(v).replace("\n", " ")
    quoted = json.dumps  # a value holding ': ' must be quoted or the note's YAML breaks in Obsidian
    if v.action == "promote":
        _atomic_write(src, set_front(text, {"status": "promoted", "promoted": day, "evidence": quoted(evidence)}))
        play = _playbook_text(vault, label).rstrip("\n")
        play = _remove_section(play, v.lesson.title)
        play += (f"\n\n## [[{v.lesson.title}]]\nPromoted {day}. {v.lesson.summary}\n\nEvidence at promotion: {evidence}\n")
        _atomic_write(_playbook_path(vault), play)
        touched.append(_playbook_path(vault))
    elif v.action in ("archive", "rollback"):
        dest = vault / LESSONS / "Archive" / src.name
        reason = f"{v.why}. {evidence}"
        _atomic_write(dest, set_front(text, {"status": "archived", "archived": day, "archive_reason": quoted(reason)}))
        src.unlink()
        touched.append(dest)
        if v.action == "rollback" and _playbook_path(vault).exists():
            _atomic_write(_playbook_path(vault), _remove_section(_playbook_path(vault).read_text(), v.lesson.title).rstrip("\n") + "\n")
            touched.append(_playbook_path(vault))
    touched.append(_append_log(vault, day, v, label, journal_path))
    return touched


def git_commit(paths: list[Path], message: str) -> str:
    """Commit exactly these paths (nothing else that is staged or changed) and return the short commit id."""
    cwd = paths[0].parent
    root = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, check=True, capture_output=True,
                               text=True).stdout.strip())
    rel = sorted({str(p.resolve().relative_to(root.resolve())) for p in paths})
    subprocess.run(["git", "add", "-A", "--", *rel], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", message, "--only", "--", *rel], cwd=root, check=True, capture_output=True)
    return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, check=True, capture_output=True,
                          text=True).stdout.strip()


# -- the run ------------------------------------------------------------------------------------

def run(desk: Path, vault: Path, as_of: str, *, apply: bool = False, commit: bool = False) -> tuple[str, list[Verdict]]:
    journal = Journal(desk)
    trades = journal.closed_trades()
    seeded = {bool(t.get("seeded")) for t in trades}
    if len(seeded) > 1:
        raise SystemExit("The journal mixes seeded (made-up) and real trades. Refusing to compare them.")
    label = SEED_TAG if seeded == {True} else ""
    if label and vault.resolve() == VAULT.resolve():
        raise SystemExit("Seeded trades must never be applied to the real vault; use a copy (see desk/lesson_demo.py).")
    setups = trade_setups(vault, trades)
    lessons = read_lessons(vault)
    verdicts = [judge(les, trades, setups, as_of) for les in lessons]
    known = {norm(les.title) for les in lessons}
    unknown: dict[str, int] = {}
    for t in trades:
        for name in t.get("lessons") or []:
            if norm(name) not in known:
                unknown[name] = unknown.get(name, 0) + 1
    unrecorded = sum(not isinstance(t.get("lessons"), list) for t in trades)
    out = render(verdicts, trades=trades, unrecorded=unrecorded, unknown=unknown, as_of=as_of, label=label)
    changes = [v for v in verdicts if v.action != "keep"]
    if not apply and not commit:
        if changes:
            out += "\n\nNothing was changed. Run with --apply to make the changes above (--commit also records them in Git)."
        return out, verdicts
    if not changes:
        return out + "\n\nNo lesson meets a rule for a change; nothing was written.", verdicts
    lines = []
    for v in changes:
        touched = apply_change(v, vault, as_of, label, _shown(desk / "journal.jsonl"))
        done = f"{v.action.upper()} {v.lesson.title}"
        if commit:
            prefix = f"{SEED_TAG}: " if label else ""
            done += f" (git {git_commit(touched, f'{prefix}Lesson tracker: {v.action} \"{v.lesson.title}\" ({as_of})')})"
        lines.append(done)
    return out + "\n\nChanges made:\n" + "\n".join(f"  - {x}" for x in lines), verdicts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--desk-dir", type=Path, default=DESK_DIR, help="folder holding journal.jsonl")
    parser.add_argument("--vault", type=Path, default=VAULT)
    parser.add_argument("--as-of", default=datetime.now(NY).date().isoformat(), help="ignore trades after this New York date (YYYY-MM-DD)")
    parser.add_argument("--apply", action="store_true", help="make the changes (default: report only)")
    parser.add_argument("--commit", action="store_true", help="make the changes and commit them (one commit per lesson)")
    args = parser.parse_args(argv)
    date.fromisoformat(args.as_of)
    text, _ = run(args.desk_dir, args.vault, args.as_of, apply=args.apply, commit=args.commit)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
