"""The lesson tracker: what counts as applied, similar trades, the 20-trade gate, promotion, archiving and rollback."""

import json
import random
import subprocess
from pathlib import Path

import pytest

from desk import lesson_demo, lessons, memory, trades
from desk.decision import InvalidDecision, parse
from desk.journal import Journal
from desk.lessons import Stats, bootstrap_difference, judge, read_lessons, run

LESSON = "Wait for the retest"


def trade(day, r, lessons_=(), n=0, **kw):
    return {"ref": f"{day}-{n}", "entry_bar": f"{day}T14:00:00+00:00", "day": day, "result_r": r, "lessons": list(lessons_), **kw}


def days(n, start=1):
    return [f"2026-08-{d:02d}" for d in range(start, start + n)]


def sample(n_applied, n_control, applied_r=1.0, control_r=0.0, spread=0.3, seed=1):
    rng = random.Random(seed)
    out = [trade(d, round(rng.gauss(applied_r, spread), 2), [LESSON], 1) for d in days(n_applied)]
    out += [trade(d, round(rng.gauss(control_r, spread), 2), [], 2) for d in days(n_control)]
    return out


def lesson(vault, status="active", **front):
    path = vault / "Lessons" / f"{LESSON}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    extra = "".join(f"{k}: {v}\n" for k, v in front.items())
    path.write_text(f"---\ntype: lesson\nstatus: {status}\nactivated: 2026-08-01\nsummary: Wait for the retest.\n{extra}---\n# {LESSON}\n")
    return read_lessons(vault)[0]


@pytest.fixture
def vault(tmp_path):
    return tmp_path / "vault"


def setups_of(ts, name="unknown"):
    return {t["ref"]: name for t in ts}


# -- recording ---------------------------------------------------------------------------------

def test_a_decision_names_its_lessons_and_rejects_a_malformed_list():
    base = {"action": "wait", "size_r": 0, "stop": None, "target": None, "reason": "x"}
    assert parse({**base, "lessons": [" A ", "A", "B"]}).lessons == ("A", "B")
    for bad in ("A", [1], [""], ["x" * 81], list("abcdef"), None):
        with pytest.raises(InvalidDecision):
            parse({**base, "lessons": bad})
    with pytest.raises(InvalidDecision):  # the key is required, like every other key
        parse(base)


def test_a_trade_collects_the_lessons_named_from_its_buy_to_its_sale():
    t = trades.open_trade({"bar": "2026-08-03T14:00:00+00:00"}, {"price": 100, "shares": 5}, {"lessons": ["A"], "stop": 99})
    trades.add_exit(t, shares=2, price=101, kind="sell", at="x", lessons=("A", "B"))
    trades.add_exit(t, shares=3, price=102, kind="seatbelt", at="y")
    assert t["lessons"] == ["A", "B"]
    assert trades.closed_record(t, protection_fills=[], now="z")["trade_closed"]["lessons"] == ["A", "B"]


# -- statistics --------------------------------------------------------------------------------

def test_stats_and_the_bootstrap_are_reproducible():
    s = Stats.of([1.0, 2.0, 3.0])
    assert (s.n, s.mean, s.sd) == (3, 2.0, 1.0) and s.se == pytest.approx(1 / 3 ** 0.5)
    assert Stats.of([]).mean is None and Stats.of([1.0]).sd is None
    a, b = [1.0, 2.0, 3.0, 2.5], [0.0, 0.5, 0.2, 0.1]
    assert bootstrap_difference(a, b, "x") == bootstrap_difference(a, b, "x")
    lo, hi = bootstrap_difference(a, b, "x")
    assert lo < 2.1 - 0.2 < hi and bootstrap_difference([1.0], b, "x") is None


# -- verdicts ----------------------------------------------------------------------------------

def test_twenty_applications_with_a_clear_edge_promote_and_nineteen_do_not(vault):
    les = lesson(vault)
    ts = sample(20, 30)
    assert judge(les, ts, setups_of(ts), "2026-12-31").action == "promote"
    ts = sample(19, 30)
    v = judge(les, ts, setups_of(ts), "2026-12-31")
    assert v.action == "keep" and "19 of the 20" in v.why and "looks good" in v.why


def test_no_clear_difference_keeps_and_a_clear_loss_archives(vault):
    les = lesson(vault)
    ts = sample(25, 30, applied_r=0.0, spread=1.0)
    assert judge(les, ts, setups_of(ts), "2026-12-31").action == "keep"
    ts = sample(12, 30, applied_r=-1.0)
    assert judge(les, ts, setups_of(ts), "2026-12-31").action == "archive"


def test_too_few_similar_trades_gives_no_verdict(vault):
    les = lesson(vault)
    ts = sample(30, 5)
    v = judge(les, ts, setups_of(ts), "2026-12-31")
    assert v.action == "keep" and "comparison group" in v.why


def test_unrecorded_unscored_and_out_of_window_trades_are_in_no_group(vault):
    les = lesson(vault)
    ts = sample(20, 30)
    old = [{**trade("2026-07-01", -5.0), "lessons": None} for _ in range(40)]  # before lessons were recorded
    old = [{k: v for k, v in t.items() if k != "lessons"} | {"ref": f"old{i}"} for i, t in enumerate(old)]
    early = [trade("2026-07-15", -5.0, [], 7) | {"ref": f"early{i}"} for i in range(40)]  # before activation (2026-08-01)
    blank = [trade("2026-08-05", None, [LESSON], 8) | {"ref": "blank"}]
    allt = ts + old + early + blank
    v = judge(les, allt, setups_of(allt), "2026-12-31")
    assert (v.applied.n, v.control.n, v.unscored) == (20, 30, 1) and v.action == "promote"


def test_controls_must_share_a_setup_with_the_applied_trades(vault):
    les = lesson(vault)
    ts = sample(20, 30)
    setups = {t["ref"]: ("breakout" if LESSON in t["lessons"] or i % 2 else "vwap") for i, t in enumerate(ts)}
    v = judge(les, ts, setups, "2026-12-31")
    assert v.setups == ["breakout"] and v.control.n == sum(1 for t in ts if LESSON not in t["lessons"] and setups[t["ref"]] == "breakout")


def test_a_promoted_lesson_is_judged_only_on_trades_after_its_promotion_day(vault):
    les = lesson(vault, "promoted", promoted="2026-08-20")
    great = [trade(d, 3.0, [LESSON], 1) for d in days(18)]  # the evidence that promoted it: must not count again
    good_controls = [trade(d, 0.0, [], 2) for d in days(18)]
    later_bad = [trade(f"2026-09-{d:02d}", -1.0, [LESSON], 3) for d in range(1, 13)]
    later_ctrl = [trade(f"2026-09-{d:02d}", 0.0, [], 4) for d in range(1, 13)]
    ts = great + good_controls + later_bad + later_ctrl
    v = judge(les, ts, setups_of(ts), "2026-12-31")
    assert v.since == "2026-08-21" and v.applied.n == 12 and v.action == "rollback"
    assert judge(les, great + good_controls, setups_of(great), "2026-12-31").action == "keep"


def test_as_of_ignores_later_trades(vault):
    les = lesson(vault)
    ts = sample(30, 30)
    assert judge(les, ts, setups_of(ts), "2026-08-10").applied.n == 10


# -- applying ----------------------------------------------------------------------------------

def write_journal(desk, ts):
    desk.mkdir(parents=True, exist_ok=True)
    (desk / "journal.jsonl").write_text("".join(json.dumps({"trade_closed": t, "at": t["entry_bar"]}) + "\n" for t in ts))


def test_report_changes_nothing_and_apply_promotes_then_rolls_back(tmp_path, vault):
    lesson(vault)
    desk = tmp_path / "desk"
    write_journal(desk, sample(25, 30))
    before = (vault / "Lessons" / f"{LESSON}.md").read_text()
    text, _ = run(desk, vault, "2026-12-31")
    assert "PROMOTE" in text and "Nothing was changed" in text and (vault / "Lessons" / f"{LESSON}.md").read_text() == before
    text, _ = run(desk, vault, "2026-12-31", apply=True)
    note = (vault / "Lessons" / f"{LESSON}.md").read_text()
    assert "status: promoted" in note and "promoted: 2026-12-31" in note and 'evidence: "applied:' in note
    assert f"[[{LESSON}]]" in (vault / "Playbook" / "Promoted lessons.md").read_text()
    log = (vault / "Changelog" / "Rules change log.md").read_text()
    assert "PROMOTED" in log and "2026-12-31" in log and "difference" in log
    assert memory.active_lessons(vault)[0].text.startswith(f"- {LESSON}:")  # a promoted lesson still reaches the trader
    assert "KEEP" in run(desk, vault, "2026-12-31")[0]  # nothing more to do on a second run

    more = [trade(f"2027-01-{d:02d}", -1.0, [LESSON], 3) | {"ref": f"b{d}"} for d in range(1, 15)]
    more += [trade(f"2027-01-{d:02d}", 0.0, [], 4) | {"ref": f"c{d}"} for d in range(1, 15)]
    write_journal(desk, sample(25, 30) + more)
    run(desk, vault, "2027-02-01", apply=True)
    assert not (vault / "Lessons" / f"{LESSON}.md").exists()
    archived = (vault / "Lessons" / "Archive" / f"{LESSON}.md").read_text()
    assert "status: archived" in archived and "archived: 2027-02-01" in archived and "archive_reason:" in archived
    assert f"[[{LESSON}]]" not in (vault / "Playbook" / "Promoted lessons.md").read_text()
    assert "ROLLED BACK" in (vault / "Changelog" / "Rules change log.md").read_text()
    assert memory.active_lessons(vault) == []


def test_lesson_names_that_match_nothing_are_listed(tmp_path, vault):
    lesson(vault)
    desk = tmp_path / "desk"
    write_journal(desk, [trade("2026-08-03", 1.0, ["Wait for the retes"])])
    assert "'Wait for the retes' x1" in run(desk, vault, "2026-12-31")[0]


def test_seeded_trades_never_touch_the_real_vault_or_mix_with_real_ones(tmp_path, vault):
    desk = tmp_path / "desk"
    write_journal(desk, [trade("2026-08-03", 1.0, [], 1, seeded=True)])
    with pytest.raises(SystemExit):
        run(desk, lessons.VAULT, "2026-12-31")
    write_journal(desk, [trade("2026-08-03", 1.0, [], 1, seeded=True), trade("2026-08-04", 1.0, [], 2)])
    with pytest.raises(SystemExit):
        run(desk, vault, "2026-12-31")


def test_empty_journal_and_no_lessons_say_so(tmp_path, vault):
    text, verdicts = run(tmp_path / "desk", vault, "2026-12-31")
    assert verdicts == [] and "Closed trades in the journal: 0" in text and "nothing to compare" in text


def test_commit_records_only_the_touched_paths(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
        subprocess.run(["git", "config", k, v], cwd=tmp_path, check=True)
    (tmp_path / "other.txt").write_text("x")
    subprocess.run(["git", "add", "other.txt"], cwd=tmp_path, check=True)
    vault = tmp_path / "vault"
    lesson(vault)
    desk = tmp_path / "desk"
    write_journal(desk, sample(25, 30))
    text, _ = run(desk, vault, "2026-12-31", commit=True)
    assert "(git " in text
    files = subprocess.run(["git", "show", "--name-only", "--format=%s", "HEAD"], cwd=tmp_path, capture_output=True, text=True).stdout
    assert "Lesson tracker: promote" in files and "Changelog/Rules change log.md" in files and "other.txt" not in files


# -- the demo ----------------------------------------------------------------------------------

def test_the_seeded_demo_shows_one_promotion_then_one_rollback_and_is_labelled(tmp_path):
    desk, vault = lesson_demo.build(tmp_path / "demo")
    first, _ = run(desk, vault, lesson_demo.PROMOTION_RUN, apply=True)
    assert "PROMOTE   SEED Wait for the retest" in first and "KEEP      SEED Skip the first bar" in first
    second, _ = run(desk, vault, lesson_demo.ROLLBACK_RUN, apply=True)
    assert "ROLLBACK  SEED Wait for the retest" in second and "KEEP      SEED Skip the first bar" in second
    log = (vault / "Changelog" / "Rules change log.md").read_text()
    assert log.count("DEMO (seeded, made-up data)") >= 3 and log.index("PROMOTED") < log.index("ROLLED BACK")
    assert all(json.loads(l)["seeded"] for l in (desk / "journal.jsonl").read_text().splitlines())
