"""The MCP trade tools: same gates and risk limits as the heartbeat, own orders only, a stop that never gets wider."""

import json

import pandas as pd
import pytest

from mcp_fakes import anyio_backend, data, mcp_rig  # noqa: F401
from desk.mcp_server import trade
from desk.mcp_server.core import MCP_TAG

pytestmark = pytest.mark.anyio

OPEN_LIMITS = """
symbols:
  SPY: {max_shares: 10, max_position_value: 8000}
max_trades_per_day: 4
daily_loss_limit: 500
"""


def bracket(rig, **over):
    args = {"size_r": 0.5, "stop": round(rig.ref - 3, 2), "target": round(rig.ref + 8, 2), "reason": "test: pullback to ema9"}
    return {**args, **over}


def orders(rig, kind=None):
    return [o for _, o in rig.ib.placed if kind is None or o.orderType == kind]


def mine(rig, kind):
    return next(t for t in rig.ib.openTrades() if t.order.orderType == kind and t.order.action == "SELL")


async def buy_one(c, rig, **over):
    out = await c.call_tool("place_bracket", bracket(rig, **over))
    assert not out.is_error, out.content[0].text
    return data(out)


# -- place_bracket ---------------------------------------------------------------------------

async def test_bracket_goes_through_the_gate_and_is_sized_in_r(mcp_rig):
    rig = mcp_rig()
    async with rig.client() as c:
        out = await buy_one(c, rig, size_r=1.0, stop=round(rig.ref - 30, 2), target=round(rig.ref + 90, 2))
    # 1R = $1,000; risk per share ~33 -> 30 shares wanted; the lab's limit of 10 shares cuts it
    assert out["status"] == "bought" and out["shares"] == 10
    assert any("size cut from 30 to 10" in w for w in out["warnings"])
    assert out["risk_dollars"] == pytest.approx(10 * (out["price"] - out["stop"]), abs=0.01)
    assert out["reward_to_risk"] > 2 and "DELAYED" in " ".join(out["warnings"])
    parent, stop, target = orders(rig)
    assert (parent.action, parent.orderType, stop.orderType, target.orderType) == ("BUY", "LMT", "STP", "LMT")
    assert parent.orderRef.startswith(MCP_TAG + ":") and stop.orderRef == target.orderRef == parent.orderRef
    assert stop.ocaGroup == target.ocaGroup and stop.parentId == parent.orderId
    # its own state and journal, never the heartbeat's
    assert rig.desk.state.path.parent == rig.desk.root and rig.desk.state.entry_day == "2026-10-21"
    assert parent.orderRef in rig.desk.state.sent_refs
    rec = rig.journal()[-1]
    assert rec["tool"] == "place_bracket" and rec["outcome"] == "done" and rec["request"]["reason"].startswith("test")


async def test_bracket_without_a_target_needs_a_reason(mcp_rig):
    rig = mcp_rig()
    async with rig.client() as c:
        no = await c.call_tool("place_bracket", {"size_r": 0.5, "stop": rig.ref - 3, "reason": "x"})
        assert no.is_error and "no_target_reason" in no.content[0].text and orders(rig) == []
        ok = await c.call_tool("place_bracket", {"size_r": 0.5, "stop": rig.ref - 3, "reason": "x", "no_target_reason": "trailing by hand"})
    assert not ok.is_error and [o.orderType for o in orders(rig)] == ["LMT", "STP"]


@pytest.mark.parametrize("over, text", [
    ({"size_r": 1.5}, "less than or equal to 1"),        # above 1R: refused before it reaches the validator
    ({"size_r": 0}, "greater than 0"),
    ({"stop": 0}, "greater than 0"),
    ({"reason": ""}, "at least 1"),
    ({"reason": "x" * 281}, "at most 280"),
])
async def test_bracket_input_is_checked_before_anything_happens(mcp_rig, over, text):
    rig = mcp_rig()
    async with rig.client() as c:
        out = await c.call_tool("place_bracket", bracket(rig, **over))
    assert out.is_error and text in out.content[0].text and orders(rig) == []


@pytest.mark.parametrize("over, text", [
    ({"stop": 700.0}, "not below the price"),
    ({"target": 500.0}, "not above the price"),
])
async def test_bracket_prices_must_straddle_the_last_close(mcp_rig, over, text):
    rig = mcp_rig()
    async with rig.client() as c:
        out = await c.call_tool("place_bracket", bracket(rig, **over))
    assert out.is_error and text in out.content[0].text and orders(rig) == []
    assert rig.journal()[-1]["outcome"] == "refused"


async def test_bracket_refused_by_the_stop_file(mcp_rig):
    rig = mcp_rig()
    rig.stop_file()
    async with rig.client() as c:
        out = await c.call_tool("place_bracket", bracket(rig))
    assert out.is_error and "STOP file" in out.content[0].text and orders(rig) == []


async def test_bracket_refused_on_a_stale_bar_when_closed_and_near_the_close(mcp_rig):
    async def attempt(rig):
        async with rig.client() as c:
            return await c.call_tool("place_bracket", bracket(rig))

    stale = mcp_rig(minutes_after_bar=45)  # the newest closed bar is 45 min old (limit 30)
    out = await attempt(stale)
    assert out.is_error and "45" in out.content[0].text and "entry_window" in out.content[0].text

    closed = mcp_rig(shown=234, minutes_after_bar=1)  # the 15:55 bar has closed at 16:00; now is 16:01 -> market shut
    out = await attempt(closed)
    assert out.is_error and "closed" in out.content[0].text

    late = mcp_rig(shown=233, minutes_after_bar=1)  # newest bar 15:50 (closed 15:55), now 15:56: 4 min to the close
    out = await attempt(late)
    assert out.is_error and "within 10 min" in out.content[0].text
    for rig in (stale, closed, late):
        assert orders(rig) == []


async def test_bracket_refused_when_the_lab_limits_leave_no_room(mcp_rig):
    rig = mcp_rig(limits="symbols:\n  QQQ: {max_shares: 10, max_position_value: 8000}\nmax_trades_per_day: 4\ndaily_loss_limit: 500\n")
    async with rig.client() as c:
        out = await c.call_tool("place_bracket", bracket(rig))
    assert out.is_error and "risk limits" in out.content[0].text and orders(rig) == []


async def test_bracket_never_adds_to_a_position(mcp_rig):
    rig = mcp_rig()
    async with rig.client() as c:
        await buy_one(c, rig)
        again = await c.call_tool("place_bracket", bracket(rig))
    assert again.is_error and "never adds" in again.content[0].text
    assert len(orders(rig, "LMT")) == 2  # one entry + one target, nothing more


async def test_bracket_refused_next_to_the_heartbeats_orders_or_shares(mcp_rig):
    rig = mcp_rig()
    rig.heartbeat_order()
    async with rig.client() as c:
        out = await c.call_tool("place_bracket", bracket(rig))
    assert out.is_error and "heartbeat" in out.content[0].text
    assert len(orders(rig)) == 1  # only the heartbeat's own order

    other = mcp_rig()
    other.ib.holdings["SPY"] = 4  # shares with no stop of ours
    async with other.client() as c:
        out = await c.call_tool("place_bracket", bracket(other))
    assert out.is_error and "not ours" in out.content[0].text and orders(other) == []


async def test_halted_server_refuses_entries_but_can_still_close(mcp_rig):
    rig = mcp_rig()
    async with rig.client() as c:
        await buy_one(c, rig)
        rig.desk.state.halted = "test halt"
        no = await c.call_tool("place_bracket", bracket(rig))
        assert no.is_error and "halted" in no.content[0].text
        closed = await c.call_tool("close_position", {"reason": "halt: flatten"})
    assert not closed.is_error and data(closed)["status"] == "sold" and rig.ib.holdings["SPY"] == 0


async def test_a_paper_guard_failure_halts_the_server(mcp_rig, monkeypatch):
    from live.broker import PaperGuardError
    rig = mcp_rig()
    monkeypatch.setattr("live.broker.check_paper", lambda *a, **k: (_ for _ in ()).throw(PaperGuardError("not a paper account")))
    async with rig.client() as c:
        out = await c.call_tool("place_bracket", bracket(rig))
    assert out.is_error and "HALTED" in out.content[0].text and rig.desk.state.halted
    assert json.loads(rig.desk.state.path.read_text())["halted"]


# -- modify_order ----------------------------------------------------------------------------

async def test_a_stop_only_moves_up(mcp_rig):
    rig = mcp_rig()
    async with rig.client() as c:
        await buy_one(c, rig)
        stop = mine(rig, "STP")
        old = stop.order.auxPrice
        for bad, text in ((old - 1, "only ever moves up"), (old, "only ever moves up"), (rig.ref + 1, "not below the last close")):
            out = await c.call_tool("modify_order", {"order_id": stop.order.orderId, "new_price": bad})
            assert out.is_error and text in out.content[0].text
            assert stop.order.auxPrice == old
        ok = await c.call_tool("modify_order", {"order_id": stop.order.orderId, "new_price": round(old + 1.5, 2)})
    assert not ok.is_error and data(ok)["kind"] == "stop" and stop.order.auxPrice == round(old + 1.5, 2)


async def test_a_target_must_keep_two_to_one_reward_to_risk(mcp_rig):
    rig = mcp_rig()
    async with rig.client() as c:
        await buy_one(c, rig)
        target = mine(rig, "LMT")
        too_low = await c.call_tool("modify_order", {"order_id": target.order.orderId, "new_price": round(rig.ref + 2, 2)})
        assert too_low.is_error and "at least 2" in too_low.content[0].text
        below = await c.call_tool("modify_order", {"order_id": target.order.orderId, "new_price": round(rig.ref - 1, 2)})
        assert below.is_error and "not above" in below.content[0].text
        ok = await c.call_tool("modify_order", {"order_id": target.order.orderId, "new_price": round(rig.ref + 12, 2)})
    assert not ok.is_error and data(ok)["kind"] == "target" and target.order.lmtPrice == round(rig.ref + 12, 2)


async def test_modify_and_cancel_refuse_orders_that_are_not_ours(mcp_rig):
    rig = mcp_rig()
    theirs = rig.heartbeat_order()
    async with rig.client() as c:
        for tool, args in (("modify_order", {"new_price": 595.0}), ("cancel_order", {})):
            out = await c.call_tool(tool, {"order_id": theirs.order.orderId, **args})
            assert out.is_error and "heartbeat" in out.content[0].text and "only lets the client that placed" in out.content[0].text
        missing = await c.call_tool("cancel_order", {"order_id": 999})
    assert missing.is_error and "no open SPY order" in missing.content[0].text
    assert theirs.orderStatus.status != "Cancelled" and rig.ib.cancelled == []


async def test_an_entry_cannot_be_modified(mcp_rig):
    rig = mcp_rig()
    rig.ib.freeze = True  # the entry never fills and stays resting
    async with rig.client() as c:
        await c.call_tool("place_bracket", bracket(rig))
        entry = next(t for t in rig.ib.trades() if t.order.action == "BUY")
        out = await c.call_tool("modify_order", {"order_id": entry.order.orderId, "new_price": 590.0})
    assert out.is_error and ("only the protective stop" in out.content[0].text or "no open" in out.content[0].text)


# -- cancel_order ----------------------------------------------------------------------------

async def test_the_protective_stop_cannot_be_cancelled_but_the_target_can(mcp_rig):
    rig = mcp_rig()
    async with rig.client() as c:
        await buy_one(c, rig)
        stop, target = mine(rig, "STP"), mine(rig, "LMT")
        no = await c.call_tool("cancel_order", {"order_id": stop.order.orderId})
        assert no.is_error and "unprotected" in no.content[0].text and not stop.isDone()
        yes = await c.call_tool("cancel_order", {"order_id": target.order.orderId})
    assert not yes.is_error and data(yes)["status"] == "cancelled" and target.isDone() and not stop.isDone()


# -- close_position --------------------------------------------------------------------------

async def test_close_sells_everything_and_cancels_its_protection(mcp_rig):
    rig = mcp_rig()
    async with rig.client() as c:
        await buy_one(c, rig)
        out = await c.call_tool("close_position", {"reason": "idea is wrong: broke below ema21"})
    res = data(out)
    assert res["status"] == "sold" and res["shares"] == 10 and rig.ib.holdings["SPY"] == 0
    assert all(t.isDone() for t in rig.ib.trades() if t.order.action == "SELL")  # the stop and target are gone
    assert rig.desk.state.entry_day is None
    assert [r["tool"] for r in rig.journal()] == ["place_bracket", "close_position"]


async def test_close_refuses_when_flat_when_not_ours_and_when_the_market_is_shut(mcp_rig):
    rig = mcp_rig()
    async with rig.client() as c:
        flat = await c.call_tool("close_position", {"reason": "nothing"})
        assert flat.is_error and "no long position" in flat.content[0].text
        rig.ib.holdings["SPY"] = 5
        rig.heartbeat_order()
        theirs = await c.call_tool("close_position", {"reason": "not mine"})
    assert theirs.is_error and "not protected by orders of this server" in theirs.content[0].text and rig.ib.holdings["SPY"] == 5

    shut = mcp_rig()
    async with shut.client() as c:
        await buy_one(c, shut)
        shut.now = pd.Timestamp("2026-10-21 20:30", tz="UTC")  # 16:30 New York
        shut.desk.parts(shut.ib).feed.shown = 234
        out = await c.call_tool("close_position", {"reason": "after hours"})
    assert out.is_error and "market is closed" in out.content[0].text and shut.ib.holdings["SPY"] == 10


# -- the wall-clock seatbelt -----------------------------------------------------------------

def tick(rig):
    return trade.seatbelt_tick(rig.ib, rig.desk)


async def test_seatbelt_sells_our_position_fifteen_minutes_before_the_close(mcp_rig):
    rig = mcp_rig(shown=225)  # newest closed bar 15:10
    async with rig.client() as c:  # (leaving the block closes the connection, so the ticks run inside it)
        await buy_one(c, rig)
        rig.now = pd.Timestamp("2026-10-21 15:40", tz="America/New_York")  # 20 min to the close: nothing yet
        assert tick(rig) is None and rig.ib.holdings["SPY"] == 10
        rig.now = pd.Timestamp("2026-10-21 15:46", tz="America/New_York")  # 14 min to the close: 5 min before the heartbeat's own
        done = tick(rig)
    assert done["seatbelt"] == "flatten_before_close" and done["result"]["status"] == "sold"
    assert rig.ib.holdings["SPY"] == 0 and rig.desk.state.entry_day is None
    assert rig.journal()[-1]["seatbelt"] == "flatten_before_close"


async def test_seatbelt_sells_a_position_carried_overnight_and_cancels_orphans(mcp_rig):
    rig = mcp_rig()
    async with rig.client() as c:
        await buy_one(c, rig)
        rig.now = pd.Timestamp("2026-10-22 10:00", tz="America/New_York")  # next morning, still long
        rig.desk.state.entry_day = "2026-10-21"
        done = tick(rig)
    assert done["seatbelt"] == "overnight_carry" and rig.ib.holdings["SPY"] == 0

    orphan = mcp_rig()
    async with orphan.client() as c:
        await buy_one(c, orphan)
        orphan.ib.holdings["SPY"] = 0  # the position vanished (sold elsewhere) but the GTC stop/target still rest
        done = tick(orphan)
    assert done["seatbelt"] == "orphan_protection" and not done["result"]["problems"]
    assert all(t.isDone() for t in orphan.ib.trades() if t.order.action == "SELL")


async def test_seatbelt_leaves_a_resting_entrys_children_alone(mcp_rig):
    from types import SimpleNamespace
    from ib_async import LimitOrder, StopOrder
    rig = mcp_rig()
    rig.ib.freeze = True  # the entry stays resting at IB
    spy = SimpleNamespace(symbol="SPY")
    ref = f"{MCP_TAG}:2026-10-21:1245"
    entry = LimitOrder("BUY", 5, 600.0)
    entry.orderRef = ref
    parent = rig.ib.placeOrder(spy, entry)
    stop = StopOrder("SELL", 5, 590.0)
    stop.orderRef, stop.parentId = ref, entry.orderId
    child = rig.ib.placeOrder(spy, stop)
    async with rig.client():
        assert tick(rig) is None  # a stop whose entry has not filled yet is not an orphan
    assert not parent.isDone() and not child.isDone()


# -- the heartbeat and the MCP server never trade SPY side by side -------------------------------------

async def test_mcp_server_refuses_everything_while_the_heartbeat_holds_spy(mcp_rig):
    from desk.mcp_server.core import HEARTBEAT_TAG
    from live.broker import Broker
    from live.execution import Executor
    rig = mcp_rig()
    heartbeat = Executor(rig.ib, Broker(rig.ib, HEARTBEAT_TAG, risk=rig.risk), rig.desk.contract(rig.ib, "SPY"), timeout_s=2)
    assert heartbeat.enter(f"{HEARTBEAT_TAG}:2026-10-21:1245", 5, rig.ref, round(rig.ref - 3, 2), round(rig.ref + 8, 2))
    sent, theirs = len(rig.ib.placed), [t for t in rig.ib.openTrades() if t.order.action == "SELL"]
    async with rig.client() as c:
        placed = await c.call_tool("place_bracket", bracket(rig))
        closed = await c.call_tool("close_position", {"reason": "flatten the heartbeat's shares"})
        modified = await c.call_tool("modify_order", {"order_id": theirs[0].order.orderId, "new_price": round(rig.ref - 1, 2)})
        cancelled = await c.call_tool("cancel_order", {"order_id": theirs[0].order.orderId})
    for out in (placed, closed, modified, cancelled):
        text = out.content[0].text
        assert out.is_error and ("heartbeat" in text.lower() or "not protected by orders of this server" in text)
    assert len(rig.ib.placed) == sent and rig.ib.holdings["SPY"] == 5 and rig.ib.cancelled == []
    assert all(not t.isDone() for t in theirs)  # the heartbeat's stop and target are untouched
