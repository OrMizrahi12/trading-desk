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

    def __init__(self, answer):
        self.answer, self.commands, self.facts = answer, [], []

    def __call__(self, cmd, input=None, **kw):
        self.commands.append(cmd)
        facts = json.loads(input)
        self.facts.append(facts)
        out = self.answer(facts)
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"structured_output": out}), "")


def trade_answer(facts):
    if facts["kind"] == "trade":
        return {"why_bought": "Price closed back above VWAP (the day's volume-weighted average price).",
                "what_happened": f"The desk closed by hand at {facts['exit_price']}.", "good_trade": True,
                "setup": "VWAP reclaim", "lesson": "nothing to change"}
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
                     "exit_price": "603.0", "exit_type": "close", "result_r": "1.0", "classification": "good trade good result"}
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
                "Trades/../Rules/x.md", ".obsidian/app.json", "Trades/x.txt"):
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
