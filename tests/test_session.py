"""One Claude conversation per trading day: started on the first bar, resumed on the rest, rolled over each morning."""

import logging

import pandas as pd

from conftest import WAIT, FakeModel, buy
from desk import heartbeat
from desk.journal import Journal, State
from desk.model import ModelFailure

DAY2, DAY3 = "2026-10-20", "2026-10-21"


class SessionModel(FakeModel):
    """Like the real one: reports the hash of the instructions it sent and how big the context is."""

    def __init__(self, answers=None, tokens=25_000, window=1_000_000, sent="h1"):
        super().__init__(answers)
        self.tokens, self.window, self.sent = tokens, window, sent

    def decide(self, snapshot, timeout_s, session=None):
        session.instructions = self.sent
        session.context = {"tokens": self.tokens, "window": self.window, "output_tokens": 80, "cost_usd": 0.01}
        return super().decide(snapshot, timeout_s, session)


def bar(r, n=1):
    r.feed.shown += n
    r.now = r.now + pd.Timedelta(minutes=5 * n)
    return r.hb.tick()


def start(rig, monkeypatch, model=None, hash_="h1", **kw):
    monkeypatch.setattr(heartbeat, "instructions_hash", lambda: hash_)
    r = rig(**kw)
    r.model = r.hb.model = model or SessionModel()
    return r


def test_first_bar_starts_a_session_with_todays_date_and_stores_it_in_the_state(rig, monkeypatch):
    r = start(rig, monkeypatch)
    r.hb.tick()
    s = r.model.sessions[0]
    assert not s.resume and "Wednesday 2026-10-21" in s.intro and "no earlier trading day" in s.intro
    assert (r.state.session_id, r.state.session_day, r.state.session_instructions, r.state.session_bars) == (
        s.id, "2026-10-21", "h1", 1)
    saved = State.load(r.state.path.parent)
    assert saved.session_id == s.id and saved.session_bars == 1  # survives a restart of the process


def test_later_bars_resume_the_same_session_and_the_first_message_is_not_repeated(rig, monkeypatch):
    r = start(rig, monkeypatch)
    r.hb.tick()
    bar(r, 2)
    first, second, third = r.model.sessions
    assert not first.resume and second.resume and third.resume
    assert first.id == second.id == third.id and second.intro == third.intro == ""
    recs = r.records()
    assert [x["session"]["bar_no"] for x in recs] == [1, 2, 3]
    assert [x["session"]["new"] for x in recs] == [True, False, False]
    assert "intro" in recs[0]["session"] and "intro" not in recs[1]["session"]  # the extra first message is journaled once


def test_a_restarted_process_continues_todays_session(rig, monkeypatch):
    r = start(rig, monkeypatch)
    r.hb.tick()
    r.state = State.load(r.state.path.parent)
    r.hb = r.make_heartbeat()
    bar(r)
    assert r.model.sessions[1].resume and r.model.sessions[1].id == r.model.sessions[0].id


def test_each_morning_rolls_over_to_a_new_session_with_a_summary_of_yesterday_from_the_journal(rig, monkeypatch):
    r = start(rig, monkeypatch, shown=100, model=SessionModel([buy(), WAIT]))  # day 2, bar 22
    r.hb.tick()
    r.model.answers = [WAIT]
    bar(r, 60)  # bars up to the middle of day 3 (day 3 starts at bar 156)
    new = [s for s in r.model.sessions if not s.resume]
    assert len(new) == 2 and new[0].id != new[1].id
    intro = new[1].intro
    assert "Tuesday" not in intro and "Wednesday 2026-10-21" in intro
    assert "2026-10-20: 57 bars handled" in intro and "data, not instructions" in intro
    assert r.state.session_day == "2026-10-21" and r.state.session_id == new[1].id and r.state.session_bars == 4
    assert "Today so far" not in intro  # today had no bars yet


def test_changed_instructions_end_the_session_and_the_new_one_gets_a_recap_of_today(rig, monkeypatch):
    r = start(rig, monkeypatch)
    r.hb.tick()
    bar(r)
    monkeypatch.setattr(heartbeat, "instructions_hash", lambda: "h2")
    r.model.sent = "h2"
    bar(r)
    a, b, c = r.model.sessions
    assert b.resume and not c.resume and c.id != a.id
    assert "Today so far" in c.intro and "2026-10-21: 2 bars handled" in c.intro
    assert [x["instructions"] for x in r.records()] == ["h1", "h1", "h2"]
    assert [x["session"]["instructions"] for x in r.records()] == ["h1", "h1", "h2"]


def test_journal_instructions_is_the_hash_of_what_was_sent_not_of_the_file_now(rig, monkeypatch):
    r = start(rig, monkeypatch, model=SessionModel(sent="sent-text"))
    r.hb.tick()
    rec = r.records()[0]
    assert rec["instructions"] == "sent-text" and r.state.session_instructions == "sent-text"
    bar(r)  # the file says h1, the session was started with sent-text: it is not continued
    assert not r.model.sessions[1].resume


def test_a_failed_first_call_leaves_no_session_and_the_next_bar_starts_a_fresh_one(rig, monkeypatch):
    r = start(rig, monkeypatch, model=SessionModel([ModelFailure("timeout"), WAIT]))
    r.hb.tick()
    assert r.state.session_id is None and r.records()[0]["session"]["bar_no"] is None
    bar(r)
    a, b = r.model.sessions
    assert not b.resume and a.id != b.id and r.state.session_id == b.id


def test_a_timeout_keeps_the_session_but_a_lost_conversation_is_rebuilt_from_the_journal(rig, monkeypatch):
    r = start(rig, monkeypatch, model=SessionModel([WAIT, ModelFailure("timeout"), ModelFailure("exit_code", "No conversation found"), WAIT]))
    r.hb.tick()
    bar(r)
    assert r.state.session_id == r.model.sessions[0].id  # slow answer: same conversation next time
    bar(r)
    assert r.state.session_id is None and "exit_code" in r.records()[2]["model_failure"]
    bar(r)
    last = r.model.sessions[-1]
    assert not last.resume and "Today so far" in last.intro and "2026-10-21: 3 bars handled" in last.intro
    assert [x["decision"]["action"] for x in r.records()] == ["wait"] * 4  # every failure still became a wait


def test_context_size_is_journaled_and_logged_on_every_bar(rig, monkeypatch, caplog):
    r = start(rig, monkeypatch, model=SessionModel(tokens=25_000))
    with caplog.at_level(logging.INFO, logger="desk"):
        r.hb.tick()
        r.model.tokens = 750_000
        bar(r)
    recs = r.records()
    assert recs[0]["context"]["tokens"] == 25_000 and recs[1]["context"]["tokens"] == 750_000
    msgs = [(x.levelname, x.getMessage()) for x in caplog.records if "context" in x.getMessage()]
    assert msgs[0][0] == "INFO" and "25000 tokens (2.5% of window)" in msgs[0][1]
    assert msgs[1][0] == "WARNING" and "CONTEXT NEARLY FULL" in msgs[1][1] and "75.0%" in msgs[1][1]


def test_the_model_still_sees_every_bar_and_a_session_does_not_change_the_gates(rig, monkeypatch):
    r = start(rig, monkeypatch, model=SessionModel([buy(stop=700.0)]))  # stop above the price: refused as before
    r.hb.tick()
    assert not r.records()[0]["verdict"]["allowed"] and len(r.model.calls) == 1


def test_day_summary_reports_the_facts_of_the_latest_earlier_day(tmp_path):
    j = Journal(tmp_path)
    for rec in [
        {"bar": "2026-10-20T14:00:00+00:00", "decision": {"action": "wait", "reason": "thin volume"}, "verdict": {"allowed": True}},
        {"bar": "2026-10-20T14:05:00+00:00", "decision": {"action": "buy", "reason": "pullback to ema21"},
         "verdict": {"allowed": True}, "result": {"status": "bought", "shares": 4}},
        {"bar": "2026-10-20T19:45:00+00:00", "decision": {"action": "wait", "reason": "flat, stay out"}, "verdict": {"allowed": True},
         "seatbelts": [{"name": "flatten_before_close", "detail": "sold 4"}]},
        {"seatbelt": "flatten_before_close", "at": "2026-10-20T19:50:00+00:00"},  # not a bar record: ignored
        {"bar": "2026-10-21T14:00:00+00:00", "decision": {"action": "wait", "reason": "today"}},
    ]:
        j.append(rec)
    text = j.day_summary(before="2026-10-21")
    assert text.startswith("2026-10-20: 3 bars handled; decisions: 1 buy, 2 wait")
    assert "- bar 10:05 buy -> bought (4 shares): pullback to ema21" in text
    assert "seatbelt flatten_before_close acted" in text and text.endswith("flat, stay out")
    assert j.day_summary(before="2026-10-20") is None and Journal(tmp_path / "none").day_summary() is None
