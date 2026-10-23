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

    def __init__(self, answers=None, tokens=25_000, window=1_000_000, sent="h1", step=0.02):
        super().__init__(answers)
        self.tokens, self.window, self.sent, self.step, self.totals = tokens, window, sent, step, {}

    def decide(self, snapshot, timeout_s, session=None):
        session.instructions = self.sent
        self.totals[session.id] = self.totals.get(session.id, 0) + self.step  # like claude: a running total per session
        session.context = {"tokens": self.tokens, "window": self.window, "output_tokens": 80, "session_cost_usd": self.totals[session.id]}
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


# -- later bars send only what changed -----------------------------------------------------------

import json  # noqa: E402


def test_first_bar_gets_the_full_snapshot_and_later_bars_only_what_changed(rig, monkeypatch):
    r = start(rig, monkeypatch)
    r.hb.tick()
    bar(r)
    full, delta = r.model.calls
    assert "bars_5min" in full and "snapshot_kind" not in full
    assert delta["snapshot_kind"] == "delta" and "bars_5min" not in delta and "bars_daily" not in delta
    prev, now_ = full["session"]["bar_start"], delta["session"]["bar_start"]
    assert [b["start"] for b in delta["new_bars_5min"]] == [now_] and prev != now_
    assert delta["new_bars_5min"][0]["ema9"] is not None and "rsi14" in delta["new_bars_5min"][0]  # with its indicators
    assert len(delta["today_daily_bar"]) == 1
    for k in ("session", "positions", "open_orders", "account", "stop_file_present", "risk_limits", "todays_fills"):
        assert k in delta
    assert delta["session"]["minutes_to_close"] < full["session"]["minutes_to_close"]  # the clock is current
    a, b = r.records()
    assert (a["input"], b["input"]) == ("full", "delta") and b["input_bytes"] < a["input_bytes"] / 5
    assert json.loads(open(b["input_file"]).read()) == delta  # exactly what the trader saw is kept
    assert b["snapshot_sha1"] and "snapshot_file" in b  # the full snapshot is still built, hashed and saved


def test_the_intro_tells_the_trader_that_later_bars_are_deltas_and_it_is_journaled(rig, monkeypatch):
    r = start(rig, monkeypatch)
    r.hb.tick()
    assert "only what changed" in r.model.sessions[0].intro and "only what changed" in r.records()[0]["session"]["intro"]


def test_the_delta_carries_the_desks_verdict_on_the_last_decision_which_the_session_never_saw(rig, monkeypatch):
    r = start(rig, monkeypatch, model=SessionModel([buy(stop=700.0), WAIT]))  # refused: stop above price
    r.hb.tick()
    bar(r)
    last = r.model.calls[1]["recent_journal"][-1]
    assert last["decision"]["action"] == "buy" and last["verdict"]["allowed"] is False


def test_delta_follows_the_live_position_and_orders(rig, monkeypatch):
    r = start(rig, monkeypatch, model=SessionModel([buy(), WAIT]))
    r.hb.tick()
    bar(r)
    d = r.model.calls[1]
    assert d["positions"] and d["positions"][0]["symbol"] == "SPY" and len(d["open_orders"]) == 2
    assert d["recent_journal"][-1]["result"]["status"] == "bought"


def test_a_bar_the_session_never_answered_is_not_lost_from_the_next_delta(rig, monkeypatch):
    r = start(rig, monkeypatch, model=SessionModel([WAIT, ModelFailure("timeout"), WAIT]))
    r.hb.tick()
    bar(r)  # timed out: the session did not answer it
    bar(r)
    d = r.model.calls[2]
    assert [b["start"] for b in d["new_bars_5min"]] == [r.model.calls[1]["session"]["bar_start"], d["session"]["bar_start"]]


def test_three_bars_closed_while_away_arrive_in_order_each_as_a_delta_of_its_own(rig, monkeypatch):
    r = start(rig, monkeypatch)
    r.hb.tick()
    bar(r, 3)
    kinds = [c.get("snapshot_kind", "full") for c in r.model.calls]
    assert kinds == ["full", "delta", "delta", "delta"]
    assert [len(c["new_bars_5min"]) for c in r.model.calls[1:]] == [1, 1, 1]


def test_a_previous_bar_older_than_the_history_falls_back_to_the_full_snapshot(rig, monkeypatch):
    r = start(rig, monkeypatch)
    r.hb.tick()
    r.state.session_last_bar = "2026-10-01T14:00:00+00:00"
    bar(r)
    assert "bars_5min" in r.model.calls[1] and r.model.sessions[1].resume and r.records()[1]["input"] == "full"


def test_a_new_session_after_a_lost_one_gets_the_full_snapshot_again(rig, monkeypatch):
    r = start(rig, monkeypatch, model=SessionModel([WAIT, ModelFailure("exit_code", "No conversation found"), WAIT]))
    r.hb.tick()
    bar(r)
    bar(r)
    assert [c.get("snapshot_kind", "full") for c in r.model.calls] == ["full", "delta", "full"]


def test_a_nearly_full_context_ends_the_session_ourselves_instead_of_leaving_it_to_compaction(rig, monkeypatch, caplog):
    r = start(rig, monkeypatch, model=SessionModel(tokens=820_000))
    r.hb.tick()
    with caplog.at_level(logging.WARNING, logger="desk"):
        r.model.tokens = 90_000
        bar(r)
        bar(r)
    a, b, c = r.model.sessions
    assert not b.resume and a.id != b.id and "Today so far" in b.intro and "2026-10-21: 1 bars handled" in b.intro
    assert c.resume and c.id == b.id  # the new one is small, so it carries on
    assert any("instead of letting claude compact" in x.getMessage() for x in caplog.records)
    assert r.model.calls[1].get("snapshot_kind") is None  # and it starts with the full snapshot


# -- cost: claude reports a running total per session, the journal needs each bar's own cost -------

def test_each_bar_journals_its_own_cost_and_the_running_total(rig, monkeypatch, caplog):
    r = start(rig, monkeypatch, model=SessionModel(step=0.02))
    with caplog.at_level(logging.INFO, logger="desk"):
        r.hb.tick()
        bar(r, 2)
    ctx = [x["context"] for x in r.records()]
    assert [c["bar_cost_usd"] for c in ctx] == [0.02, 0.02, 0.02]
    assert [round(c["session_cost_usd"], 2) for c in ctx] == [0.02, 0.04, 0.06]
    assert round(r.state.session_cost_usd, 2) == 0.06
    assert any("cost $0.0200 this bar, $0.0600 this session" in x.getMessage() for x in caplog.records)


def test_a_new_session_starts_its_cost_from_zero_and_does_not_inherit_the_old_total(rig, monkeypatch):
    r = start(rig, monkeypatch, model=SessionModel(step=0.05))
    r.hb.tick()
    bar(r)
    monkeypatch.setattr(heartbeat, "instructions_hash", lambda: "h2")
    r.model.sent = "h2"
    bar(r)  # new session: its first call costs 0.05, not 0.15 - 0.10
    ctx = [x["context"] for x in r.records()]
    assert [c["bar_cost_usd"] for c in ctx] == [0.05, 0.05, 0.05] and ctx[2]["session_cost_usd"] == 0.05


def test_cost_of_a_session_from_before_tracking_is_unknown_for_one_bar_then_exact(rig, monkeypatch):
    r = start(rig, monkeypatch, model=SessionModel(step=0.03))
    r.hb.tick()
    r.state.session_cost_usd = None  # state file written by the previous version
    r.model.totals[r.state.session_id] = 0.5
    bar(r)
    bar(r)
    c = [x["context"] for x in r.records()]
    assert c[1]["bar_cost_usd"] is None and round(c[1]["session_cost_usd"], 2) == 0.53
    assert round(c[2]["bar_cost_usd"], 2) == 0.03


def test_the_cost_total_survives_a_restart(rig, monkeypatch):
    r = start(rig, monkeypatch, model=SessionModel(step=0.04))
    r.hb.tick()
    r.state = State.load(r.state.path.parent)
    r.hb = r.make_heartbeat()
    bar(r)
    assert [round(x["context"]["bar_cost_usd"], 2) for x in r.records()] == [0.04, 0.04]
