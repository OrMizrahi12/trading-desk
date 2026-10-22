"""The trade tools' logic, kept apart from the read tools. Each is `f(ib, desk, **args)` and runs on the IB thread.

Every order goes through the same chain as the heartbeat's: `decision.parse` (the schema) -> `gates.gate` (STOP file,
session, bar age, no adding, sizing in R, the lab's risk limits) -> `execute.run` (the lab's Executor / Broker, which
check paper-ness and risk again before anything is sent). On top of that this module only manages orders it placed
itself (tag `live:mcp:SPY`); a position or resting order of the heartbeat is refused, never touched.

Terms:
- bracket: an entry order sent together with a protective stop and a profit target; when one of stop/target fills the
  other is cancelled (OCA = one-cancels-all).
- R: 1% of net liquidation (total account value). `size_r` = how many R the trade risks if its stop is hit.
- reward-to-risk: how far the target is above the price divided by how far the stop is below it.
"""

import functools
import logging
import math

import pandas as pd

from .. import lab  # noqa: F401
from live.broker import PaperGuardError
from live.config import FLATTEN_BEFORE_CLOSE_MIN
from live.execution import DuplicateEntry, ExecutionHalt, OrderRejected
from live.reconcile import ReconcileResult, cancel_confirmed, open_orders
from live.risk import RiskRefused, net_liquidation

from .. import execute, gates, limits_pin, seatbelts, snapshot
from ..decision import Decision, InvalidDecision, parse
from .core import DELAY_NOTE, MCP_TAG, SYMBOL, Desk, Refused, owner_of

log = logging.getLogger("desk.mcp")

MAX_SIZE_R = 1.0  # CLAUDE.md: "Never above 1R, even though the schema allows 2R" (the heartbeat enforces that in the prompt only)
EARLY_FLATTEN_MIN = 5  # this server sells 5 minutes before the heartbeat's own 10-minute seatbelt would, so they never race
TERMS = {"stop": "resting order that sells at your loss limit", "target": "resting order that sells at your profit goal",
         "R": "1% of net liquidation (total account value): the loss if the stop is hit",
         "reward_to_risk": "distance from price to target divided by distance from price to stop"}


def journaled(tool: str):
    """Write every call, whatever it ends in, to the server's journal; turn lab errors into plain refusals."""

    def deco(fn):
        @functools.wraps(fn)
        def inner(ib, desk: Desk, **req):
            try:
                out = fn(ib, desk, **req)
            except Refused as exc:
                desk.log(tool, req, "refused", reason=str(exc))
                raise
            except RiskRefused as exc:
                desk.log(tool, req, "refused", reason=f"risk limits: {exc.reason}")
                raise Refused(f"the lab's risk limits refused it: {exc.reason}") from exc
            except DuplicateEntry as exc:
                desk.log(tool, req, "refused", reason=str(exc))
                raise Refused(f"this entry was already sent ({exc.ref}); check get_open_orders and get_positions") from exc
            except OrderRejected as exc:
                desk.log(tool, req, "refused", reason=f"IB rejected: {exc}")
                raise Refused(f"IB rejected the order: {exc}") from exc
            except (PaperGuardError, ExecutionHalt) as exc:
                _halt(desk, f"{tool}: {type(exc).__name__}: {exc}")
                desk.log(tool, req, "halted", reason=str(exc))
                raise Refused(f"HALTED, new entries are blocked until a person clears it: {exc}") from exc
            except Exception as exc:
                desk.log(tool, req, "error", error=f"{type(exc).__name__}: {exc}")
                raise
            desk.log(tool, req, "done", result=out)
            return out

        return inner

    return deco


def _halt(desk: Desk, why: str) -> None:
    log.error("MCP trading HALT: %s", why)
    desk.state.halted = why
    desk.state.save()


def _entries_allowed(desk: Desk) -> None:
    if desk.state.halted:
        raise Refused(f"new entries and changes are halted: {desk.state.halted}. A person must look, then clear "
                      f"'halted' in {desk.state.path}. close_position still works.")


def _my_order(ib, desk: Desk, order_id: int):
    for t in open_orders(ib, SYMBOL):
        if t.order.orderId == order_id and not t.isDone():
            owner = owner_of(t.order.orderRef)
            if owner != "mcp":
                raise Refused(f"order {order_id} belongs to the {owner}; IB only lets the client that placed an order "
                              f"change or cancel it, so it cannot be touched from here")
            return t
    raise Refused(f"no open {SYMBOL} order with id {order_id} (see get_open_orders)")


def _net_liq(ib) -> float | None:
    try:
        return net_liquidation(ib)
    except Exception:
        return None


@journaled("place_bracket")
def place_bracket(ib, desk: Desk, *, size_r: float, stop: float, target: float | None, reason: str,
                  no_target_reason: str | None = None) -> dict:
    _entries_allowed(desk)
    if desk.pinned:  # re-checked on every entry: the file may have been swapped since the server started
        try:
            limits_pin.verify()
        except limits_pin.LimitsTampered as exc:
            raise Refused(f"{exc}. New entries are blocked; close_position still works.") from exc
    if size_r > MAX_SIZE_R:
        raise Refused(f"size_r {size_r} is above {MAX_SIZE_R}R: the desk never risks more than 1R on one trade")
    if target is None and not (no_target_reason or "").strip():
        raise Refused("a target is expected: give one, or explain in no_target_reason why there is none")
    try:
        decision = parse({"action": "buy", "size_r": size_r, "stop": stop, "target": target, "reason": reason})
    except InvalidDecision as exc:
        raise Refused(f"invalid order: {exc}") from exc
    own = desk.ownership(ib)
    if own.foreign:
        ids = sorted(t.order.orderId for t in own.foreign)
        raise Refused(f"orders {ids} on {SYMBOL} belong to the heartbeat or another client; this server will not trade "
                      f"alongside them (IB would not let it cancel them)")
    if own.shares > 0 and own.state == "foreign":
        raise Refused(f"{own.shares} {SYMBOL} shares are held but this server placed no stop for them: not ours to manage")
    if own.state != "flat":
        raise Refused("already long, or an entry of ours is resting: the desk never adds to a position")
    mkt = desk.market(ib)
    parts = desk.parts(ib)
    resting_buys = sum(1 for t in ib.openTrades() if t.order.action == "BUY" and t.contract.symbol == SYMBOL and not t.isDone())
    verdict = gates.gate(decision, ref_price=mkt.ref_price, held=own.shares, resting_buys=resting_buys, session=mkt.session,
                         net_liq=_net_liq(ib), position_stop=None, risk=desk.risk, ib=ib, symbol=SYMBOL,
                         stop_file=gates.stop_file_present(desk.risk.root))
    if not verdict.allowed:
        raise Refused(verdict.reason + (f" [seatbelt: {verdict.seatbelt}]" if verdict.seatbelt else ""))
    result = execute.run(decision, verdict.shares, executor=parts.executor, ib=ib, tag=MCP_TAG, symbol=SYMBOL,
                         bar_ts=mkt.bar_start, ref_price=mkt.ref_price, state=desk.state)
    out = {**result, "stop": decision.stop, "target": decision.target, "size_r": decision.size_r,
           "ref_price": mkt.ref_price, "ref_bar_start": mkt.bar_start.tz_convert(snapshot.NY).isoformat(),
           "ref_bar_age_minutes": mkt.session["bar_age_minutes"], "terms": TERMS, "warnings": [DELAY_NOTE]}
    if verdict.note:
        out["warnings"].append(verdict.note)
    if result.get("status") == "bought":
        desk.state.entry_day = str(mkt.now.tz_convert(snapshot.NY).date())
        desk.state.save()
        price = result["price"]
        out["risk_dollars"] = round(result["shares"] * (price - decision.stop), 2)
        if decision.target is not None and price > decision.stop:
            out["reward_to_risk"] = round((decision.target - price) / (price - decision.stop), 2)
            if out["reward_to_risk"] < 2:
                out["warnings"].append("reward-to-risk is below 2, which CLAUDE.md asks for (not enforced in code)")
    else:
        out["warnings"].append(f"entry status is {result.get('status')!r}: nothing is held; check get_open_orders")
    return out


@journaled("modify_order")
def modify_order(ib, desk: Desk, *, order_id: int, new_price: float) -> dict:
    """Raise the resting stop, or move the target. The stop never moves down or gets wider."""
    _entries_allowed(desk)
    trade = _my_order(ib, desk, order_id)
    o = trade.order
    if o.action != "SELL" or o.orderType not in ("STP", "LMT"):
        raise Refused("only the protective stop or the target can be modified; to change an entry, cancel it and place a new bracket")
    if isinstance(new_price, bool) or not math.isfinite(new_price) or new_price <= 0:
        raise Refused("new_price must be a positive number")
    new = round(float(new_price), 2)
    ref = desk.market(ib).ref_price
    if o.orderType == "STP":
        old = float(o.auxPrice)
        if new <= old:
            raise Refused(f"a stop only ever moves up: {old} -> {new} would keep it or widen it (if the idea is wrong, close_position)")
        if new >= ref:
            raise Refused(f"the new stop {new} is not below the last close {ref}: it would be hit at once")
        o.auxPrice, kind = new, "stop"
    else:
        old = float(o.lmtPrice)
        if new <= ref:
            raise Refused(f"the new target {new} is not above the last close {ref}")
        stop = next((float(t.order.auxPrice) for t in desk.ownership(ib).mine if t.order.orderType == "STP"), None)
        if stop is not None and ref > stop and (new - ref) < 2 * (ref - stop):
            raise Refused(f"target {new} is only {new - ref:.2f} above the price while the stop risks {ref - stop:.2f}: "
                          f"reward-to-risk must stay at least 2")
        o.lmtPrice, kind = new, "target"
    broker = desk.parts(ib).broker
    broker.place(desk.parts(ib).contract, o, ref=o.orderRef)
    ib.sleep(1.0)  # let IB answer; a refused change shows up as an error on the order id
    if broker.refused(trade):
        raise Refused(f"IB refused the change: {broker.rejection(trade)}")
    return {"status": "modified", "order_id": order_id, "kind": kind, "old_price": old, "new_price": new, "ref_price": ref,
            "warnings": [DELAY_NOTE]}


@journaled("cancel_order")
def cancel_order(ib, desk: Desk, *, order_id: int) -> dict:
    trade = _my_order(ib, desk, order_id)
    o, own = trade.order, desk.ownership(ib)
    if o.action == "SELL" and o.orderType == "STP" and (own.shares > 0 or any(t.order.action == "BUY" for t in own.mine)):
        raise Refused("that is the protective stop of an open position or pending entry: cancelling it would leave the "
                      "position unprotected. Use modify_order to raise it, or close_position to exit")
    if o.action == "BUY" and trade.orderStatus.filled > 0:
        raise Refused("that entry is partly filled: cancelling it would leave a position; use close_position")
    res = ReconcileResult()
    if not cancel_confirmed(ib, desk.parts(ib).broker, trade, res, "cancelled via the MCP server"):
        raise Refused("; ".join(res.problems) + " (check get_open_orders before doing anything else)")
    children = [t for t in open_orders(ib, SYMBOL) if getattr(t.order, "parentId", 0) == o.orderId]
    return {"status": "cancelled", "order_id": order_id, "actions": res.actions,
            "children_still_open": [t.order.orderId for t in children if not t.isDone()]}


@journaled("close_position")
def close_position(ib, desk: Desk, *, reason: str) -> dict:
    try:
        decision = parse({"action": "close", "size_r": 0, "stop": None, "target": None, "reason": reason})
    except InvalidDecision as exc:
        raise Refused(f"invalid request: {exc}") from exc
    own = desk.ownership(ib)
    if own.shares <= 0:
        raise Refused("no long position to close" + ("; an entry of ours is resting, use cancel_order" if own.mine else ""))
    if own.state != "mine":
        raise Refused(f"the {own.shares} {SYMBOL} shares held are not protected by orders of this server (the heartbeat or "
                      f"another client owns them): selling here could leave its stop live on shares already sold")
    mkt = desk.market(ib)
    if not mkt.session.get("market_open_now"):
        raise Refused("the market is closed: a sell order would not fill. Try again during regular hours")
    verdict = gates.gate(decision, ref_price=mkt.ref_price, held=own.shares, resting_buys=0, session=mkt.session, net_liq=None,
                         position_stop=None, risk=desk.risk, ib=ib, symbol=SYMBOL, stop_file=False)
    if not verdict.allowed:
        raise Refused(verdict.reason)
    out = execute.run(decision, 0, executor=desk.parts(ib).executor, ib=ib, tag=MCP_TAG, symbol=SYMBOL,
                      bar_ts=mkt.bar_start, ref_price=mkt.ref_price, state=desk.state)
    if out.get("status") == "sold":
        desk.state.entry_day = None
        desk.state.save()
    return {**out, "ref_price": mkt.ref_price, "warnings": [DELAY_NOTE]}


# -- the wall-clock seatbelt ----------------------------------------------------------------------


def _flatten_reason(now: pd.Timestamp, entry_day: str | None) -> str | None:
    """The heartbeat's rule (sell 10 min before the close, never hold overnight) but 5 minutes earlier."""
    why = seatbelts.flatten_reason(now, entry_day)
    if why:
        return why
    day = pd.Timestamp(now.tz_convert(snapshot.NY).date())
    cal = snapshot._CAL
    if cal.is_session(day) and cal.session_open(day) <= now < cal.session_close(day) and \
            now >= cal.session_close(day) - pd.Timedelta(minutes=FLATTEN_BEFORE_CLOSE_MIN + EARLY_FLATTEN_MIN):
        return "flatten_before_close"
    return None


def seatbelt_tick(ib, desk: Desk) -> dict | None:
    """Called every ~30 s by the server. Sells our position before the close, clears our orphaned orders, and says what it did.

    Without this a bracket's GTC (good-till-cancelled) stop would outlive the day, and the desk never holds overnight.
    """
    own = desk.ownership(ib)
    has_entry = any(t.order.action == "BUY" for t in own.mine)
    acted = None
    if own.state == "mine" and own.shares > 0:
        entry_day = desk.state.entry_day or next((t.order.orderRef.split(":")[3] for t in own.mine if t.order.orderRef.count(":") >= 3), None)
        why = _flatten_reason(desk.clock(), entry_day)
        if why:
            record = {"seatbelt": why, "held": own.shares}
            try:
                price = desk.market(ib).ref_price
                record["result"] = execute.reduce_position(desk.parts(ib).executor, ib, MCP_TAG, SYMBOL, own.shares, price)
                if record["result"].get("status") in ("sold", "nothing_to_sell"):
                    desk.state.entry_day = None
                    desk.state.save()
            except Refused as exc:  # no price yet: say so and try again on the next tick, without halting
                record["error"] = str(exc)
            except (PaperGuardError, ExecutionHalt) as exc:
                _halt(desk, f"seatbelt {why}: {exc}")
                record["halted"] = str(exc)
            acted = record
    elif own.shares == 0 and not has_entry:
        stale = [t for t in own.mine if t.order.action == "SELL"]  # a stop/target with nothing left to protect could open a short
        if stale:
            res = ReconcileResult()
            for t in stale:
                cancel_confirmed(ib, desk.parts(ib).broker, t, res, "orphaned stop/target, nothing held")
            acted = {"seatbelt": "orphan_protection", "result": {"actions": res.actions, "problems": res.problems}}
        if desk.state.entry_day is not None:
            desk.state.entry_day = None
            desk.state.save()
    if acted:
        acted["at"] = desk.now_iso()
        log.warning("SEATBELT %s", acted)
        desk.journal.append(acted)
    return acted
