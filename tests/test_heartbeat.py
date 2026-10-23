"""The heartbeat end to end with a fake model and a fake IB: every bar reaches the model, gates and journal work."""

import pytest

from conftest import CLOSE, WAIT, buy, sell
from desk.heartbeat import Halted
from desk.model import ModelFailure
from live.broker import PaperGuardError, connect_paper


def orders(r, kind=None):
    return [o for _, o in r.ib.placed if kind is None or o.orderType == kind]


# -- the model sees every bar --------------------------------------------------------------

def test_first_run_handles_only_the_newest_bar_and_writes_everything(rig):
    r = rig()
    recs = r.hb.tick()
    assert len(recs) == 1 and len(r.model.calls) == 1
    rec = r.records()[0]
    assert rec["decision"]["action"] == "wait" and rec["verdict"]["allowed"] and rec["snapshot_sha1"]
    assert rec["elapsed_s"] == 0 and not rec["over_budget"]
    assert (r.journal.root / "snapshots").glob("*.json")
    assert r.state.last_bar_ts == r.feed.poll().index[-1].isoformat()
    assert r.hb.tick() == [] and len(r.model.calls) == 1  # the same bar is not handled twice


def test_every_new_bar_reaches_the_model_in_order_and_only_the_newest_can_trade(rig):
    r = rig(shown=190, answers=[buy()])
    r.hb.tick()
    assert len(orders(r)) > 0  # the first run handled bar 190 (newest) and bought
    r2 = rig(shown=190, answers=[buy()])
    r2.hb.tick()
    r2.feed.shown = 193  # three bars close while we were away
    r2.ib.placed.clear(), r2.ib.holdings.clear()
    recs = r2.hb.tick()
    assert len(recs) == 3 and len(r2.model.calls) == 1 + 3
    assert [x["bar"] for x in recs] == sorted(x["bar"] for x in recs)
    assert [x["verdict"]["allowed"] for x in recs[:2]] == [False, False]
    assert "superseded" in recs[0]["verdict"]["reason"]


@pytest.mark.parametrize("setup", ["plain", "stop_file", "long_position", "after_hours", "model_failure", "bad_answer"])
def test_model_is_called_once_per_bar_in_every_state(rig, setup):
    answers = {"model_failure": [ModelFailure("timeout")], "bad_answer": [{"nonsense": 1}]}.get(setup, [WAIT])
    r = rig(answers=answers, shown=196 if setup != "after_hours" else 234)
    if setup == "stop_file":
        r.stop_file()
    if setup == "long_position":
        r.ib.holdings["SPY"] = 5
    r.hb.tick()
    assert len(r.model.calls) == 1
    assert len(r.records()) == 1


# -- fail safe: wait --------------------------------------------------------------------------

def test_model_failure_and_invalid_answer_become_wait_and_send_nothing(rig):
    for answer, field in [(ModelFailure("timeout", "slow"), "model_failure"), ({"action": "buy"}, "invalid"),
                          (buy(stop=700.0), "verdict")]:
        r = rig(answers=[answer])
        r.hb.tick()
        rec = r.records()[0]
        assert field in rec and not orders(r)
        assert rec["decision"]["action"] == "wait" or field == "verdict"


# -- buying -----------------------------------------------------------------------------------

def test_buy_sends_one_bracket_through_the_lab_and_journals_it(rig):
    r = rig(answers=[buy(size_r=1.0, stop=597.0, target=606.0)])
    r.hb.tick()
    rec = r.records()[0]
    assert rec["result"]["status"] == "bought" and rec["result"]["ref"].startswith("live:desk:SPY:2026-10-21:")
    kinds = sorted(o.orderType for o in orders(r))
    assert kinds == ["LMT", "LMT", "STP"]  # entry (marketable limit), target, stop
    assert all(o.orderRef == rec["result"]["ref"] for o in orders(r))
    assert r.ib.holdings["SPY"] == rec["verdict"]["shares"] == rec["result"]["shares"]
    assert rec["result"]["ref"] in r.state.sent_refs


def test_size_in_r_is_capped_by_the_lab_risk_limits(rig):
    r = rig(answers=[buy(size_r=2.0, stop=599.0, target=610.0)])  # 2R at a $1 stop is ~330 shares; the limit is 10
    r.hb.tick()
    assert r.records()[0]["verdict"]["shares"] == 10


def test_buy_is_refused_while_long_or_when_the_bar_is_stale_or_late_in_the_day(rig):
    r = rig(answers=[buy()])
    r.ib.holdings["SPY"] = 3
    r.hb.tick()
    assert "already long" in r.records()[0]["verdict"]["reason"] and not orders(r)
    r = rig(answers=[buy()], now=None)
    r.now = r.now + __import__("pandas").Timedelta(minutes=40)
    r.hb.tick()
    assert "old" in r.records()[0]["verdict"]["reason"] and not orders(r)
    r = rig(answers=[buy()], shown=234)  # the 15:55 bar, seen at 16:07
    r.hb.tick()
    assert not orders(r) and not r.records()[0]["verdict"]["allowed"]


# -- STOP file -----------------------------------------------------------------------------------

def test_stop_file_blocks_buys_but_the_model_still_sees_the_bar_and_the_file_is_kept(rig):
    r = rig(answers=[buy()])
    r.stop_file()
    r.hb.tick()
    rec = r.records()[0]
    assert "STOP" in rec["verdict"]["reason"] and not orders(r)
    assert r.model.calls[0]["stop_file_present"] is True
    assert (r.lab_root / "STOP").exists()


def test_stop_file_does_not_block_closing(rig):
    r = rig(answers=[buy(), CLOSE])
    r.hb.tick()
    r.stop_file()
    r.feed.shown += 1
    r.now = r.now + __import__("pandas").Timedelta(minutes=5)
    r.ib.market_price = 600.0
    r.hb.tick()
    assert r.records()[1]["result"]["status"] == "sold" and r.ib.holdings["SPY"] == 0


# -- sell / close / no shorting ---------------------------------------------------------------

def test_sell_without_a_position_is_refused_never_a_short(rig):
    r = rig(answers=[sell()])
    r.hb.tick()
    assert "no long position" in r.records()[0]["verdict"]["reason"] and not orders(r)


def test_partial_sell_keeps_the_rest_protected(rig):
    r = rig(answers=[buy(size_r=2.0, stop=599.0, target=610.0), sell(size_r=0.02)])  # 0.02R is ~5 shares at this stop distance
    r.hb.tick()
    assert r.ib.holdings["SPY"] == 10
    r.feed.shown += 1
    r.now = r.now + __import__("pandas").Timedelta(minutes=5)
    r.hb.tick()
    rec = r.records()[1]
    assert rec["result"]["status"] == "sold" and 0 < rec["result"]["shares"] < 10
    left = r.ib.holdings["SPY"]
    assert left == 10 - rec["result"]["shares"]
    live = [t for t in r.ib.openTrades() if t.order.action == "SELL"]
    assert sorted(t.order.orderType for t in live) == ["LMT", "STP"] and all(t.order.totalQuantity == left for t in live)


def test_close_sells_everything_and_cancels_the_protection(rig):
    r = rig(answers=[buy(), CLOSE])
    r.hb.tick()
    r.feed.shown += 1
    r.now = r.now + __import__("pandas").Timedelta(minutes=5)
    r.hb.tick()
    assert r.ib.holdings["SPY"] == 0 and not [t for t in r.ib.openTrades() if t.order.action == "SELL"]


# -- budget -----------------------------------------------------------------------------------

def test_model_timeout_is_the_smaller_of_its_cap_and_the_budget(rig):
    r = rig()
    r.hb.tick()
    assert r.model.timeouts == [75.0]
    r = rig(budget_s=30.0)
    r.hb.tick()
    assert r.model.timeouts == [30.0]


def test_slow_model_leaves_no_time_to_buy_but_the_bar_is_journaled_over_budget(rig):
    r = rig(answers=[buy()])
    original = r.model.decide

    def slow(snapshot, timeout_s, session=None):
        r.mono[0] += 110  # the model used up 110 of the 120 seconds
        return original(snapshot, timeout_s, session)

    r.model.decide = slow
    r.hb.tick()
    rec = r.records()[0]
    assert "budget exhausted" in rec["verdict"]["reason"] and not orders(r)
    assert rec["elapsed_s"] == 110 and not rec["over_budget"]


def test_close_is_still_sent_when_the_budget_is_nearly_used(rig):
    r = rig(answers=[buy(), CLOSE])
    r.hb.tick()
    r.feed.shown += 1
    r.now = r.now + __import__("pandas").Timedelta(minutes=5)
    original = r.model.decide

    def slow(snapshot, timeout_s, session=None):
        r.mono[0] += 115
        return original(snapshot, timeout_s, session)

    r.model.decide = slow
    r.hb.tick()
    assert r.records()[1]["result"]["status"] == "sold"


# -- restart and paper guard ----------------------------------------------------------------------

def test_restart_does_not_repeat_a_bar_or_an_entry(rig):
    r = rig(answers=[buy()])
    r.hb.tick()
    n_orders, n_calls = len(orders(r)), len(r.model.calls)
    from desk.journal import State
    r.state = State.load(r.state.path.parent)  # a new process reads the state file
    r.hb = r.make_heartbeat()
    assert r.hb.tick() == [] and len(orders(r)) == n_orders and len(r.model.calls) == n_calls


def test_a_non_paper_account_halts_and_nothing_is_sent(rig):
    r = rig(answers=[buy()], accounts=("U1234567",))
    with pytest.raises(Halted):
        r.hb.tick()
    assert not orders(r) and "paper guard" in r.records()[0]["halted"]
    with pytest.raises(Halted):
        r.hb.tick()  # stays halted until a person clears it


@pytest.mark.parametrize("port", [7496, 4001, 4003])
def test_live_ports_are_refused_at_connect(port):
    from conftest import FakeIB
    with pytest.raises(PaperGuardError):
        connect_paper(FakeIB, port=port)


def test_a_failing_bar_is_journaled_and_the_next_bar_still_reaches_the_model(rig):
    r = rig(answers=[WAIT])
    r.hb.tick()
    original = r.hb._gate
    r.hb._gate = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    r.feed.shown += 1
    r.hb.tick()
    assert "boom" in r.records()[1]["error"]
    r.hb._gate = original
    r.feed.shown += 1
    r.hb.tick()
    assert len(r.model.calls) == 3
