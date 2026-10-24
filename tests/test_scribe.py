"""The vault writer: trade_closed records from the heartbeat, notes written from them, and the guards around Claude."""

import json
import shutil
import subprocess
from pathlib import Path

import pandas as pd
import pytest

from conftest import CLOSE, buy
from desk import scribe, trades
from desk.journal import Journal
from desk.scribe import Scribe, ScribeFailure

REAL_VAULT = Path(__file__).parent.parent / "vault"
FRIDAY = "2026-10-23"
ENTRY_BAR = "2026-10-23T14:35:00+00:00"  # 10:35 New York
EXIT_BAR = "2026-10-23T15:15:00+00:00"  # 11:15 New York


def leg(shares=10, price=603.0, kind="close"):
    return {"shares": shares, "price": price, "type": kind, "at": EXIT_BAR}


def open_(**k):
    t = {"ref": "r1", "entry_bar": ENTRY_BAR, "entry_seen_at": ENTRY_BAR, "entry_price": 600.0, "shares": 10, "stop": 597.0,
         "target": 606.0, "size_r": 0.5, "entry_reason": "VWAP reclaim: closed back above VWAP, 20-bar EMA above it", "exits": []}
    return {**t, **k}


# -- trades.closed_record ---------------------------------------------------------------------

def test_result_is_exit_minus_entry_over_the_stop_distance():
    rec = trades.closed_record({**open_(), "exits": [leg(price=603.0)]}, protection_fills=[], now="n")["trade_closed"]
    assert rec["exit_price"] == 603.0 and rec["result_r"] == 1.0 and rec["exit_type"] == "close" and rec["day"] == FRIDAY


def test_two_sales_are_averaged_by_shares():
    t = {**open_(), "exits": [leg(4, 603.0, "sell"), leg(6, 601.0, "close")]}
    rec = trades.closed_record(t, protection_fills=[], now="n")["trade_closed"]
    assert rec["exit_price"] == 601.8 and rec["exit_type"] == "mixed"


def test_a_stop_fill_is_named_by_its_price_and_nothing_is_guessed_without_one():
    fill = [{"shares": 10, "price": 596.9, "time": EXIT_BAR}]
    rec = trades.closed_record(open_(), protection_fills=fill, now="n")["trade_closed"]
    assert rec["exit_type"] == "stop" and rec["result_r"] == round((596.9 - 600) / 3, 2)
    none = trades.closed_record(open_(), protection_fills=[], now="n")["trade_closed"]
    assert none["exit_price"] is None and none["result_r"] is None and none["exit_type"] is None
    part = trades.closed_record({**open_(), "exits": [leg(4)]}, protection_fills=fill, now="n")["trade_closed"]
    assert part["exit_price"] is None  # 4 shares sold by the desk, 6 by an order whose split is unknown


# -- the heartbeat journals the end of a trade --------------------------------------------------

def test_heartbeat_journals_trade_closed_when_the_desk_closes(rig):
    r = rig(answers=[buy(size_r=1.0, stop=597.0, target=606.0), CLOSE])
    r.hb.tick()
    assert r.state.open_trade["stop"] == 597.0 and not r.journal.closed_trades()
    r.feed.shown += 1
    r.now = r.now + pd.Timedelta(minutes=5)
    r.hb.tick()
    [t] = r.journal.closed_trades()
    assert r.state.open_trade is None and t["exit_type"] == "close" and t["entry_price"] and t["exit_price"]
    assert t["result_r"] == trades.result_r(t["entry_price"], t["exit_price"], 597.0)
    assert State_reload(r).open_trade is None


def State_reload(r):
    from desk.journal import State
    return State.load(r.journal.root)


# -- the writer ---------------------------------------------------------------------------------

class FakeClaude:
    """Stands in for `claude -p`: answers from a function of (kind, facts); records every command."""

    def __init__(self, answer, skills=("desk:post-trade-review",)):
        self.answer, self.commands, self.facts, self.skills = answer, [], [], skills

    def __call__(self, cmd, input=None, **kw):
        self.commands.append(cmd)
        facts = json.loads(input)
        self.facts.append(facts)
        out = self.answer(facts)
        if cmd[cmd.index("--output-format") + 1] == "stream-json":  # the review call: one line per event, skills it loaded first
            lines = [{"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": s}}]}}
                     for s in self.skills]
            lines.append({"type": "result", "structured_output": out})
            return subprocess.CompletedProcess(cmd, 0, "\n".join(json.dumps(x) for x in lines), "")
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"structured_output": out}), "")


def trade_answer(facts):
    if facts["kind"] == "trade":
        return {"why_bought": "Price closed back above VWAP (the day's volume-weighted average price).",
                "what_happened": f"The desk closed by hand at {facts['exit_price']}.", "good_trade": True,
                "setup": "VWAP reclaim", "lesson": "nothing to change"}
    if facts["kind"] == "review":
        return {"good_trade": True, "rule_broken": "none", "lesson_tag": "exit-management",
                "review": f"The bars rose after the entry and the desk closed by hand at {facts['exit_price']}.",
                "lesson": "Close by hand only when the target is nearly reached.", "lesson_title": "Close near the target"}
    if facts["kind"] == "day":
        return {"how_the_day_went": f"One trade, {facts['trades'][0]['result_r']}R." if facts["trades"] else "A quiet day."}
    return {"summary": f"{facts['trades_closed']} trade this week."}


@pytest.fixture
def desk(tmp_path):
    vault = tmp_path / "vault"
    shutil.copytree(REAL_VAULT, vault, ignore=shutil.ignore_patterns(".obsidian", "Daily", "Trades", "Weekly"))
    (vault / "Trades").mkdir()
    journal = Journal(tmp_path / "desk")
    journal.append({"bar": ENTRY_BAR, "decision": {"action": "buy", "reason": "x"}, "result": {"status": "bought"}})
    journal.append({"bar": EXIT_BAR, "decision": {"action": "close", "reason": "target nearly reached"}, "result": {"status": "sold"}})
    journal.append(trades.closed_record({**open_(), "exits": [leg()]}, protection_fills=[], now=EXIT_BAR))
    journal.append(bar_wait(LAST_BAR))  # the day's last bar, 15:55 New York time
    return journal, vault


LAST_BAR = "2026-10-23T19:55:00+00:00"


def bar_wait(bar):
    return {"bar": bar, "decision": {"action": "wait", "reason": "market closed"}}


def after_close(day=FRIDAY):
    return pd.Timestamp(f"{day} 16:15", tz="America/New_York")


def make(desk, answer=trade_answer, **k):
    journal, vault = desk
    fake = FakeClaude(answer)
    return Scribe(journal, vault=vault, run=fake, background=False, **k), fake


def test_trade_note_comes_from_the_template_with_python_numbers_and_both_links(desk):
    s, fake = make(desk)
    s.run_pending(pd.Timestamp(f"{FRIDAY} 11:20", tz="America/New_York"))
    note = (s.vault / "Trades" / "2026-10-23 1035 SPY.md").read_text()
    front = scribe.front_dict(note)
    assert front == {"type": "trade", "date": FRIDAY, "symbol": "SPY", "setup": "vwap-reclaim", "entry_time": "10:35",
                     "entry_price": "600.0", "stop": "597.0", "target": "606.0", "size_r": "0.5", "exit_time": "11:15",
                     "exit_price": "603.0", "exit_type": "close", "result_r": "1.0", "classification": "good trade good result",
                     "reviewed": FRIDAY}
    assert "[[VWAP reclaim]]" in note and f"[[Daily/{FRIDAY}]]" in note and "[[Constitution risk rules]]" in note
    assert "- [ ] Data fresh and complete" in note  # the checklist is the template's, not ticked by the writer
    daily = (s.vault / "Daily" / f"{FRIDAY}.md").read_text()
    assert "[[2026-10-23 1035 SPY]] · VWAP reclaim · close · 1.0R" in daily and "- Entries so far: 1" in daily
    assert scribe.front_dict(daily)["result_r"] == "1.0" and scribe.front_dict(daily)["losses"] == "0"
    assert "claude" in fake.commands[0][0] and fake.commands[0][fake.commands[0].index("--tools") + 1] == ""
    assert not (s.vault / "Daily" / f"{FRIDAY}.md").read_text().count("How the day went") == 0


def test_no_note_is_written_twice(desk):
    s, fake = make(desk)
    s.run_pending(pd.Timestamp(f"{FRIDAY} 11:20", tz="America/New_York"))
    n = len(fake.commands)
    assert s.run_pending(pd.Timestamp(f"{FRIDAY} 11:25", tz="America/New_York")) == [] and len(fake.commands) == n


def test_after_the_close_the_day_is_filled_in_and_friday_gets_a_week_linking_every_trade(desk):
    s, _ = make(desk)
    s.run_pending(after_close())
    daily = (s.vault / "Daily" / f"{FRIDAY}.md").read_text()
    assert "One trade, 1.0R." in daily and scribe.section_body(daily, "Plan") is not None
    week = (s.vault / "Weekly" / "2026-W43.md").read_text()
    assert "[[2026-10-23 1035 SPY]]" in week and f"[[{FRIDAY}]]" in week and "1 trade this week." in week
    assert scribe.front_dict(week)["week"] == "2026-W43" and scribe.front_dict(week)["result_r"] == "1.0"


def test_no_daily_or_weekly_note_before_the_close_or_on_other_days(desk):
    s, _ = make(desk)
    s.run_pending(pd.Timestamp(f"{FRIDAY} 15:00", tz="America/New_York"))
    assert "How the day went" in (s.vault / "Daily" / f"{FRIDAY}.md").read_text()
    assert scribe.section_body((s.vault / "Daily" / f"{FRIDAY}.md").read_text(), "How the day went").strip() == ""
    assert not (s.vault / "Weekly").exists()


def test_a_hand_written_plan_and_lessons_survive(desk):
    s, _ = make(desk)
    (s.vault / "Daily").mkdir()
    (s.vault / "Daily" / f"{FRIDAY}.md").write_text(
        "---\ntype: daily\ndate: 2026-10-23\nentries: 0\nlosses: 0\nresult_r: 0\n---\n# d\n\n## Plan\n- my own plan\n\n"
        "## Counters\n- Entries so far: 0\n- Losing trades so far: 0\n\n## Trades\n- None.\n\n"
        "## How the day went (fill in after the close)\n\n## Lessons\n- keep it\n")
    s.run_pending(after_close())
    daily = (s.vault / "Daily" / f"{FRIDAY}.md").read_text()
    assert "- my own plan" in daily and "- keep it" in daily and "- None." not in daily and "One trade" in daily


def test_a_number_that_is_not_in_the_facts_is_rejected_and_asked_again(desk):
    answers = []

    def answer(facts):
        if facts["kind"] != "trade":
            return trade_answer(facts)
        ok = trade_answer(facts)
        answers.append(1)
        return {**ok, "what_happened": "It made 7.5 dollars."} if len(answers) == 1 else ok

    s, fake = make(desk, answer)
    s.run_pending(pd.Timestamp(f"{FRIDAY} 11:20", tz="America/New_York"))
    assert len(answers) == 2 and "7.5" not in (s.vault / "Trades" / "2026-10-23 1035 SPY.md").read_text()
    assert "7.5" in fake.commands[1][2]  # the retry names the offending number


def test_a_writer_that_keeps_inventing_numbers_is_retried_then_replaced_by_python_text(desk):
    t = [0.0]
    s, fake = make(desk, lambda f: {**trade_answer(f), "what_happened": "It made 99 dollars."} if f["kind"] == "trade" else trade_answer(f),
                   mono=lambda: t[0])
    now = pd.Timestamp(f"{FRIDAY} 11:20", tz="America/New_York")
    for i in range(scribe.GIVE_UP_AFTER):
        assert s.run_pending(now) == []
        if i == 0:
            assert s.run_pending(now) == []  # backing off: no new call within the wait
        t[0] += scribe.RETRY_AFTER_S + 1
    assert len(fake.commands) == scribe.GIVE_UP_AFTER * scribe.ATTEMPTS
    s.run_pending(now)
    note = (s.vault / "Trades" / "2026-10-23 1035 SPY.md").read_text()
    assert "99" not in note and "could not produce text" in note and "result_r: 1.0" in note


def test_the_writer_cannot_write_outside_its_folders(desk):
    s, _ = make(desk)
    for bad in ("Rules/Lab risk limits.md", "Playbook/VWAP reclaim.md", "Templates/Trade note.md", "../outside.md",
                "Trades/../Rules/x.md", ".obsidian/app.json", "Trades/x.txt", "Lessons/Archive/x.md", "Lessons/x.txt"):
        with pytest.raises(ValueError):
            s._write(bad, "x")
    (s.vault / "Trades").rmdir()
    (s.vault / "Trades").symlink_to(s.vault.parent)  # a symlinked folder that leads out of the vault
    with pytest.raises(ValueError):
        s._write("Trades/x.md", "x")


def test_a_failing_claude_writes_nothing(desk):
    journal, vault = desk
    s = Scribe(journal, vault=vault, background=False,
               run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "boom"))
    assert s.run_pending(pd.Timestamp(f"{FRIDAY} 11:20", tz="America/New_York")) == []
    assert not list((vault / "Trades").glob("*.md"))


def test_today_waits_for_its_last_bar_before_the_daily_and_weekly_notes(tmp_path):
    vault = tmp_path / "vault"
    shutil.copytree(REAL_VAULT, vault, ignore=shutil.ignore_patterns(".obsidian", "Daily", "Trades", "Weekly"))
    (vault / "Trades").mkdir()
    journal = Journal(tmp_path / "desk")
    for bar in ("2026-10-23T19:45:00+00:00", "2026-10-23T19:50:00+00:00"):  # up to the 15:50 bar: 15:55 is still on its way
        journal.append(bar_wait(bar))
    fake = FakeClaude(trade_answer)
    s = Scribe(journal, vault=vault, run=fake, background=False)
    assert s.run_pending(after_close()) == [] and not fake.commands  # 16:15, but the day is one bar short
    assert not (vault / "Daily").exists() and not (vault / "Weekly").exists()
    journal.append(bar_wait(LAST_BAR))  # the feed hands over the 15:55 bar
    assert {p.parent.name for p in s.run_pending(after_close())} == {"Daily", "Weekly"}


def test_a_day_whose_last_bar_never_arrives_is_written_after_the_grace_period(tmp_path):
    vault = tmp_path / "vault"
    shutil.copytree(REAL_VAULT, vault, ignore=shutil.ignore_patterns(".obsidian", "Daily", "Trades", "Weekly"))
    journal = Journal(tmp_path / "desk")
    journal.append(bar_wait("2026-10-23T19:50:00+00:00"))  # a half day, or a bar the desk missed
    s = Scribe(journal, vault=vault, run=FakeClaude(trade_answer), background=False)
    assert s.run_pending(pd.Timestamp(f"{FRIDAY} 16:59", tz="America/New_York")) == []
    assert [p.parent.name for p in s.run_pending(pd.Timestamp(f"{FRIDAY} 17:00", tz="America/New_York"))] == ["Daily", "Weekly"]


def test_an_earlier_day_is_written_whatever_its_last_bar(tmp_path):
    vault = tmp_path / "vault"
    shutil.copytree(REAL_VAULT, vault, ignore=shutil.ignore_patterns(".obsidian", "Daily", "Trades", "Weekly"))
    journal = Journal(tmp_path / "desk")
    journal.append(bar_wait("2026-10-23T15:00:00+00:00"))  # the desk was stopped at 11:00; the next morning it catches up
    s = Scribe(journal, vault=vault, run=FakeClaude(trade_answer), background=False)
    assert [p.parent.name for p in s.run_pending(pd.Timestamp("2026-10-24 08:00", tz="America/New_York"))] == ["Daily"]


# -- the post-trade review ----------------------------------------------------------------------

NOON = pd.Timestamp(f"{FRIDAY} 11:20", tz="America/New_York")


def with_bars(journal, tmp_path):
    """A saved snapshot (as the heartbeat writes one per bar) holding 5-minute bars 10:30-11:20 New York time."""
    rows = [{"start": f"{FRIDAY}T{h:02d}:{m:02d}:00-04:00", "open": 600 + i, "high": 601 + i, "low": 599 + i, "close": 600.5 + i,
             "volume": 1000 + i, "vwap": 600.25, "ema9": 1.0}
            for i, (h, m) in enumerate((10 + (30 + 5 * k) // 60, (30 + 5 * k) % 60) for k in range(11))]
    path = tmp_path / "snap.json"
    path.write_text(json.dumps({"bars_5min": rows}))
    journal.append({"bar": EXIT_BAR, "snapshot_file": str(path), "decision": {"action": "wait", "reason": "x"}})


def test_a_closed_trade_wakes_claude_with_the_trade_and_the_bars_since_entry_and_the_skill_must_load(desk, tmp_path):
    journal, _ = desk
    with_bars(journal, tmp_path)
    s, fake = make(desk)
    s.run_pending(NOON)
    cmd = next(c for c in fake.commands if c[2].startswith("A trade has just ended"))
    assert cmd[cmd.index("--tools") + 1] == "Skill" and "--plugin-dir" in cmd  # the trader's tool and skills, nothing more
    assert cmd[cmd.index("--output-format") + 1] == "stream-json" and "post-trade-review" in cmd[2]
    facts = next(f for f in fake.facts if f["kind"] == "review")
    assert [b["start_ny"] for b in facts["bars_since_entry"]] == ["10:35", "10:40", "10:45", "10:50", "10:55", "11:00", "11:05",
                                                                  "11:10", "11:15"]  # entry bar to the exit, no bar before or after
    assert set(facts["bars_since_entry"][0]) == {"start_ny", "open", "high", "low", "close", "volume", "vwap"}
    assert facts["entry_price"] == 600.0 and facts["exit_price"] == 603.0 and facts["decisions_after_entry"]
    assert facts["counters_today"] == {"entries": 1, "trades_closed": 1, "losing_trades": 0, "ended_by_the_stop": False}
    assert facts["desk_limits"]["cooldown_bars_after_a_stop_out"] == 6 and "playbook" not in facts


def test_the_review_goes_into_the_trade_note_and_one_tagged_lesson_into_lessons(desk, tmp_path):
    s, _ = make(desk)
    s.run_pending(NOON)
    note = (s.vault / "Trades" / "2026-10-23 1035 SPY.md").read_text()
    review = scribe.section_body(note, "Review")
    assert "- Classification: good trade good result" in review and "- Rule broken: none" in review
    assert "The bars rose after the entry" in review and "post-trade-review skill" in review
    assert "[[Lessons/Close near the target]]" in review
    assert scribe.front_dict(note)["reviewed"] == FRIDAY
    lessons = list((s.vault / "Lessons").glob("*.md"))
    assert [p.name for p in lessons] == ["Close near the target.md"]
    front = scribe.front_dict(lessons[0].read_text())
    assert front == {"type": "lesson", "status": "proposed", "tags": "[lesson, exit-management, vwap-reclaim]",
                     "summary": "Close by hand only when the target is nearly reached.", "seen": "1", "last_seen": FRIDAY,
                     "trades": "[[2026-10-23 1035 SPY]]"}
    assert "**stop**" in lessons[0].read_text()  # every term explained
    assert s.journal.pending_review() is None
    [rec] = [x for x in map(json.loads, s.journal.path.read_text().splitlines()) if "trade_reviewed" in x]
    assert rec["trade_reviewed"]["key"] == "r1" and rec["trade_reviewed"]["lesson"] == "Close near the target"


def test_a_proposed_lesson_never_reaches_the_trader(desk):
    from desk import memory
    s, _ = make(desk)
    s.run_pending(NOON)
    assert memory.active_lessons(s.vault) == [] and memory.brief(s.vault) == ""
    assert not memory.measure(vault=s.vault).over


def test_no_review_is_recorded_when_the_skill_was_not_loaded(desk):
    s, fake = make(desk)
    fake.skills = ()  # claude answered without loading post-trade-review
    s.run_pending(NOON)
    assert s.journal.pending_review() is not None and not list((s.vault / "Lessons").glob("*.md"))
    note = (s.vault / "Trades" / "2026-10-23 1035 SPY.md").read_text()
    assert "reviewed" not in scribe.front_dict(note) and "post-trade-review skill" not in note
    assert any("did not load the post-trade-review skill" in c[2] for c in fake.commands[2:])  # asked again, told why


def test_a_review_with_a_made_up_number_is_asked_again(desk):
    calls = []

    def answer(facts):
        ok = trade_answer(facts)
        if facts["kind"] != "review":
            return ok
        calls.append(1)
        return {**ok, "review": "It made 12.34 dollars."} if len(calls) == 1 else ok

    s, fake = make(desk, answer)
    s.run_pending(NOON)
    assert len(calls) == 2 and s.journal.pending_review() is None
    assert "12.34" in fake.commands[-1][2] and "12.34" not in (s.vault / "Trades" / "2026-10-23 1035 SPY.md").read_text()


def test_a_lesson_too_long_for_the_memory_budget_is_asked_again(desk):
    calls = []

    def answer(facts):
        ok = trade_answer(facts)
        if facts["kind"] != "review":
            return ok
        calls.append(1)
        return {**ok, "lesson": "Wait. " * 60} if len(calls) == 1 else ok

    s, _ = make(desk, answer)
    s.run_pending(NOON)
    assert len(calls) == 2 and len(scribe.front_dict((s.vault / "Lessons" / "Close near the target.md").read_text())["summary"]) < 280


def test_nothing_to_change_closes_the_review_without_a_lesson_note(desk):
    s, _ = make(desk, lambda f: {**trade_answer(f), "lesson": "Nothing to change."} if f["kind"] == "review" else trade_answer(f))
    s.run_pending(NOON)
    assert not list((s.vault / "Lessons").glob("*.md")) and s.journal.pending_review() is None
    review = scribe.section_body((s.vault / "Trades" / "2026-10-23 1035 SPY.md").read_text(), "Review")
    assert "Nothing to change." in review and "[[Lessons/" not in review


def test_a_review_claude_cannot_make_is_retried_soon_forever_and_never_replaced_by_python_text(desk):
    t = [0.0]
    s, fake = make(desk, lambda f: trade_answer(f), mono=lambda: t[0])
    fake.skills = ()
    s.run_pending(NOON)  # the note is written, the review fails
    first = len(fake.commands)
    assert s.run_pending(NOON) == [] and len(fake.commands) == first  # backing off
    for _ in range(scribe.GIVE_UP_AFTER + 2):  # far past the point where a note falls back to Python's own text
        t[0] += scribe.REVIEW_RETRY_AFTER_S + 1
        s.run_pending(NOON)
    assert len(fake.commands) > first + scribe.GIVE_UP_AFTER and s.journal.pending_review() is not None
    fake.skills = ("desk:post-trade-review",)  # claude recovers
    t[0] += scribe.REVIEW_RETRY_AFTER_S + 1
    s.run_pending(NOON)
    assert s.journal.pending_review() is None


def test_a_second_trade_with_the_same_lesson_adds_to_seen_and_a_rerun_does_not_count_twice(desk):
    s, _ = make(desk)
    s.run_pending(NOON)
    t2 = {**s.journal.closed_trades()[0], "day": "2026-10-24", "entry_bar": "2026-10-24T14:35:00+00:00"}
    words = {"lesson_title": "Close near the target", "lesson_tag": "exit-management"}
    path = s._write_lesson(t2, words, "Close by hand only when the target is nearly reached.", "")
    s._write_lesson(t2, words, "Close by hand only when the target is nearly reached.", "")
    front = scribe.front_dict(path.read_text())
    assert front["seen"] == "2" and front["last_seen"] == "2026-10-24" and front["status"] == "proposed"
    assert front["trades"] == "[[2026-10-23 1035 SPY]], [[2026-10-24 1035 SPY]]"
    assert len(list((s.vault / "Lessons").glob("*.md"))) == 1


def test_a_lesson_title_cannot_leave_the_lessons_folder_or_touch_the_archive(desk):
    s, _ = make(desk, lambda f: {**trade_answer(f), "lesson_title": "../Rules/Lab risk limits"} if f["kind"] == "review" else trade_answer(f))
    s.run_pending(NOON)
    assert [p.name for p in (s.vault / "Lessons").glob("*.md")] == ["Rules Lab risk limits.md"]
    assert (s.vault / "Rules" / "Lab risk limits.md").read_text() == (REAL_VAULT / "Rules" / "Lab risk limits.md").read_text()
    assert scribe.lesson_title('A/B: "c"?') == "A B c" and scribe.lesson_title("x" * 99) == "x" * scribe.TITLE_MAX
