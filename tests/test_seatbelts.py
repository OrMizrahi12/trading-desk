"""Seatbelts: hard rules in code that always win over the model, and that are logged and journaled when they act."""

import logging

import pandas as pd
import pytest

from conftest import CLOSE, WAIT, buy
from desk import seatbelts
from desk.heartbeat import Halted


def ny(text):
    return pd.Timestamp(text, tz="America/New_York").tz_convert("UTC")


def orders(r):
    return [o for _, o in r.ib.placed]


def seatbelt_records(r, name=None):
    return [x for x in r.records() if "seatbelt" in x and (name is None or x["seatbelt"] == name)]


# -- the window rules --------------------------------------------------------------------------

@pytest.mark.parametrize("when, entry_day, expected", [
    ("2026-10-21 12:00", "2026-10-21", None),                      # mid-session, bought today
    ("2026-10-21 15:49", "2026-10-21", None),                      # one minute before the deadline
    ("2026-10-21 15:50", "2026-10-21", "flatten_before_close"),    # close 16:00 minus 10 minutes
    ("2026-10-21 15:59", None, "flatten_before_close"),            # a position of unknown origin is flattened too
    ("2026-10-21 16:00", "2026-10-21", None),                      # closed: nothing can be sold
    ("2026-10-22 10:00", "2026-10-21", "overnight_carry"),         # the desk's own position from yesterday
    ("2026-10-22 10:00", None, None),                              # origin unknown, mid-session: left alone
    ("2026-10-24 11:00", "2026-10-23", None),                      # Saturday: no session
])
def test_flatten_reason(when, entry_day, expected):
    assert seatbelts.flatten_reason(ny(when), entry_day) == expected


def test_half_day_uses_the_real_close():
    # 2026-11-27 (day after Thanksgiving) closes at 13:00, so the deadline is 12:50
    assert seatbelts.flatten_reason(ny("2026-11-27 12:49"), "2026-11-27") is None
    assert seatbelts.flatten_reason(ny("2026-11-27 12:50"), "2026-11-27") == "flatten_before_close"


# -- flatten before the close ----------------------------------------------------------------------

def long_position(rig, **kw):
    r = rig(answers=[buy(size_r=1.0, stop=597.0, target=606.0), WAIT], **kw)
    r.hb.tick()
    assert r.ib.holdings["SPY"] > 0 and r.state.entry_day == "2026-10-21"
    return r


def test_position_is_flattened_at_the_deadline_without_asking_the_model(rig, caplog):
    r = long_position(rig)
    calls = len(r.model.calls)
    r.now = ny("2026-10-21 15:50")
    with caplog.at_level(logging.WARNING, logger="desk"):
        r.hb.tick()  # no new bar: only the wall clock moved
    assert r.ib.holdings["SPY"] == 0
    assert not [t for t in r.ib.openTrades() if t.order.action == "SELL"]  # stop and target are gone too
    rec = seatbelt_records(r, "flatten_before_close")[0]
    assert rec["result"]["status"] == "sold" and rec["held"] > 0 and rec["at"]
    assert len(r.model.calls) == calls  # the seatbelt did not consult the model
    assert r.state.entry_day is None
    assert any("SEATBELT flatten_before_close" in m for m in caplog.messages)


def test_flatten_beats_the_stop_file_and_a_model_that_wants_to_hold(rig):
    r = long_position(rig)
    r.stop_file()
    r.now = ny("2026-10-21 15:55")
    r.hb.tick()
    assert r.ib.holdings["SPY"] == 0  # STOP blocks buys only; exits always pass


def test_flatten_happens_before_the_new_bars_are_shown_to_the_model(rig):
    r = long_position(rig)
    r.now = ny("2026-10-21 15:52")
    r.feed.shown += 1
    r.hb.tick()
    assert r.model.calls[-1]["positions"] == []  # the model sees the account as it is after the seatbelt acted
    assert len(r.model.calls) == 2  # and still sees the bar


def test_flatten_runs_even_when_the_feed_is_down(rig):
    r = long_position(rig)
    r.now = ny("2026-10-21 15:50")

    def down():
        raise ConnectionError("feed down")

    r.feed.poll = down
    with pytest.raises(ConnectionError):
        r.hb.tick()
    assert r.ib.holdings["SPY"] == 0 and seatbelt_records(r, "flatten_before_close")


def test_flat_account_logs_nothing(rig):
    r = rig()
    r.now = ny("2026-10-21 15:55")
    r.hb.tick()
    assert not seatbelt_records(r)


def test_no_price_known_halts_instead_of_guessing(rig):
    r = rig(shown=1)
    r.ib.holdings["SPY"] = 5
    r.hb._last_close = None
    r.feed.shown = 0  # empty feed: nothing to price the sale with
    r.now = ny("2026-10-21 15:55")
    with pytest.raises(Halted):
        r.hb.tick()
    assert not orders(r) and "no price" in r.state.halted


def test_a_sale_that_cannot_be_confirmed_halts_the_desk(rig):
    r = long_position(rig)
    r.ib.freeze = True  # nothing fills
    r.now = ny("2026-10-21 15:55")
    with pytest.raises(Halted):
        r.hb.tick()
    rec = seatbelt_records(r, "flatten_before_close")[0]
    assert "execution halt" in rec["halted"] and r.state.halted
    assert [t for t in r.ib.openTrades() if t.order.orderType == "STP"]  # the stop was put back: never unprotected


# -- overnight and market closed --------------------------------------------------------------------

def test_a_position_carried_from_an_earlier_day_is_sold_at_the_next_open(rig):
    r = long_position(rig)
    r.now = ny("2026-10-22 10:00")
    r.hb.tick()
    assert r.ib.holdings["SPY"] == 0
    assert seatbelt_records(r, "overnight_carry")[0]["result"]["status"] == "sold"


def test_a_position_of_unknown_origin_is_left_alone_mid_session(rig):
    r = rig()
    r.ib.holdings["SPY"] = 5
    r.hb.tick()
    assert r.ib.holdings["SPY"] == 5 and not seatbelt_records(r)


def test_position_held_while_the_market_is_shut_is_reported_once_and_not_sold(rig):
    r = long_position(rig)
    r.now = ny("2026-10-21 17:00")
    n = len(orders(r))
    r.hb.tick()
    r.hb.tick()
    recs = seatbelt_records(r)
    assert len(recs) == 1 and recs[0]["result"]["status"] == "market_closed" and len(orders(r)) == n


# -- the other seatbelts are tagged when they act --------------------------------------------------

def belts(r):
    return {b["name"]: b["detail"] for rec in r.records() for b in rec.get("seatbelts", [])}


def test_stop_file_refusal_is_tagged(rig, caplog):
    r = rig(answers=[buy()])
    r.stop_file()
    with caplog.at_level(logging.WARNING, logger="desk"):
        r.hb.tick()
    assert "stop_file" in belts(r) and any("SEATBELT stop_file" in m for m in caplog.messages)


def test_risk_limit_shrink_is_tagged_with_the_sizes(rig):
    r = rig(answers=[buy(size_r=2.0, stop=599.0, target=610.0)])
    r.hb.tick()
    assert "from" in belts(r)["risk_limits"] and "to 10 shares" in belts(r)["risk_limits"]


def test_entry_window_refusals_are_tagged(rig):
    r = rig(answers=[buy()], shown=234)  # the 15:55 bar, seen at 16:07
    r.hb.tick()
    assert "entry_window" in belts(r)
    r = rig(answers=[buy()])
    r.now = r.now + pd.Timedelta(minutes=40)
    r.hb.tick()
    assert "entry_window" in belts(r)


def test_paper_guard_is_tagged(rig):
    r = rig(answers=[buy()], accounts=("U1234567",))
    with pytest.raises(Halted):
        r.hb.tick()
    assert "paper_guard" in belts(r)


def test_ordinary_decisions_carry_no_seatbelt_tag(rig):
    r = rig(answers=[WAIT])
    r.hb.tick()
    assert "seatbelts" not in r.records()[0]


# -- the MCP server and the heartbeat never trade SPY side by side -------------------------------------

def mcp_bracket(r, shares=5):
    """The MCP server (another IB client, tag live:mcp:SPY) has bought SPY with a stop and a target."""
    from live.broker import Broker
    from live.execution import Executor
    executor = Executor(r.ib, Broker(r.ib, "live:mcp:SPY", risk=r.risk), r.contract, timeout_s=2)
    ref = r.ib.market_price
    assert executor.enter("live:mcp:SPY:2026-10-21:1245", shares, ref, round(ref - 3, 2), round(ref + 6, 2))
    assert r.ib.holdings["SPY"] == shares


def test_heartbeat_refuses_every_decision_while_the_mcp_server_holds_spy(rig):
    for answer in (buy(), CLOSE, {"action": "sell", "size_r": 0.3, "stop": None, "target": None, "reason": "trim"}):
        r = rig(answers=[answer])
        mcp_bracket(r)
        sent = len(orders(r))
        r.hb.tick()
        rec = r.records()[0]
        assert rec["verdict"]["allowed"] is False and rec["verdict"]["seatbelt"] == "mcp_active", answer["action"]
        assert "MCP server" in rec["verdict"]["reason"] and "result" not in rec
        assert "mcp_active" in belts(r)
        assert len(orders(r)) == sent and r.ib.holdings["SPY"] == 5  # nothing sent, nothing sold
        assert all(not t.isDone() for t in r.ib.trades() if t.order.action == "SELL")  # its stop and target still rest


def test_heartbeat_refuses_to_buy_while_an_mcp_entry_is_only_resting(rig):
    r = rig(answers=[buy()])
    r.ib.freeze = True  # the MCP entry has not filled
    from ib_async import LimitOrder
    from live.broker import Broker
    Broker(r.ib, "live:mcp:SPY").place(r.contract, LimitOrder("BUY", 5, 600.0), "live:mcp:SPY:2026-10-21:1245")
    r.hb.tick()
    assert r.records()[0]["verdict"]["seatbelt"] == "mcp_active" and len(orders(r)) == 1


def test_heartbeat_still_waits_and_trades_normally_without_the_mcp_server(rig):
    r = rig(answers=[WAIT])
    r.hb.tick()
    assert r.records()[0]["verdict"]["allowed"] and "seatbelts" not in r.records()[0]
    r = rig(answers=[buy()])
    r.hb.tick()
    assert r.records()[0]["result"]["status"] == "bought"  # the lab's own order refs (live:desk:...) are not the MCP server's


def test_the_wall_clock_flatten_leaves_the_mcp_servers_shares_to_the_mcp_server(rig):
    r = rig()
    mcp_bracket(r)
    r.now = ny("2026-10-21 15:50")
    n = len(orders(r))
    r.hb.tick()
    r.hb.tick()
    recs = seatbelt_records(r, "mcp_active")
    assert len(recs) == 1 and recs[0]["result"] == {"status": "left_to_mcp", "would_have_been": "flatten_before_close"}
    assert len(orders(r)) == n and r.ib.holdings["SPY"] == 5 and not r.state.halted
    assert all(not t.isDone() for t in r.ib.trades() if t.order.action == "SELL")
