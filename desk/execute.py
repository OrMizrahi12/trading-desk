"""Carrying out an allowed decision on the paper account, only through the lab's Executor / Broker / Risk.

Terms:
- bracket: an entry order sent together with its stop and target; when one of stop/target fills, the other is
  cancelled (OCA = one-cancels-all).
- marketable limit: a limit order priced slightly through the market so it fills at once but never at a wild price.
- protection: the resting stop (and target) that guard an open position.

The desk keeps no position book of its own: what we hold and which stop/target rest is read from IB every time.
"""

import logging
from dataclasses import replace

from . import lab  # noqa: F401
from live.execution import ExecutionHalt
from live.reconcile import ib_shares, open_orders
from live.refs import entry_ref, owns
from live.state import PositionState

from .decision import Decision

log = logging.getLogger("desk")


def our_protection(ib, tag: str, symbol: str) -> list:
    return [t for t in open_orders(ib, symbol) if t.order.action == "SELL" and owns(tag, t.order.orderRef or "")
            and not t.isDone()]


def position_from_ib(ib, tag: str, symbol: str) -> PositionState | None:
    """The long position and its resting stop/target as IB reports them (None when flat)."""
    shares = ib_shares(ib, symbol)
    if shares <= 0:
        return None
    stop = target = None
    stop_id = target_id = None
    for t in our_protection(ib, tag, symbol):
        if t.order.orderType == "STP":
            stop, stop_id = float(t.order.auxPrice), t.order.orderId
        elif t.order.orderType == "LMT":
            target, target_id = float(t.order.lmtPrice), t.order.orderId
    avg = next((float(p.avgCost) for p in ib.positions() if p.contract.symbol == symbol), 0.0)
    return PositionState(shares=shares, entry_time="", entry_price=avg, stop=stop or 0.0, target=target,
                         stop_order_id=stop_id, target_order_id=target_id)


def reduce_position(executor, ib, tag, symbol, shares: int, ref_price: float) -> dict:
    """Sell `shares` of the long. The stop/target are cancelled for the sale, then put back for what remains."""
    pos = position_from_ib(ib, tag, symbol)
    if pos is None:
        return {"status": "nothing_to_sell"}
    try:
        fill = executor.exit(replace(pos, shares=min(shares, pos.shares)), ref_price, restore_protection=False)
    finally:
        remaining = ib_shares(ib, symbol)
        if remaining > 0 and pos.stop > 0 and not our_protection(ib, tag, symbol):
            executor.protect(replace(pos, shares=remaining))  # never leave a position without its stop
    if fill is None:
        return {"status": "nothing_sold" if not executor.broker.dry_run else "dry_run"}
    return {"status": "sold", "shares": fill.shares, "price": round(fill.price, 4)}


def run(decision: Decision, shares: int, *, executor, ib, tag: str, symbol: str, bar_ts, ref_price: float, state) -> dict:
    """Execute a buy / sell / close. Raises ExecutionHalt, OrderRejected, RiskRefused, DuplicateEntry for the caller."""
    if decision.action == "buy":
        ref = entry_ref(tag, bar_ts)

        def write_ahead():  # remember the ref on disk before anything is sent, so a restart never sends it twice
            state.sent_refs = [r for r in state.sent_refs if r.split(":")[3] == ref.split(":")[3]] + [ref]
            state.save()

        entered = executor.enter(ref, shares, ref_price, decision.stop, decision.target, known_refs=state.sent_refs,
                                 before_send=write_ahead)
        if entered is None:
            return {"status": "dry_run" if executor.broker.dry_run else "not_filled", "ref": ref}
        fill = entered.fill
        out = {"status": "bought", "ref": ref, "shares": fill.shares, "price": round(fill.price, 4)}
        pos = PositionState(shares=fill.shares, entry_time=fill.time.isoformat(), entry_price=fill.price,
                            stop=decision.stop, target=decision.target)
        try:
            if not entered.protected:  # IB refused the bracket's stop: place the pair on its own
                executor.protect(pos)
                out["protection"] = "placed separately"
            if fill.price <= decision.stop:
                raise ExecutionHalt(f"filled at {fill.price}, not above the stop {decision.stop}")
        except BaseException:
            executor.exit(pos, ref_price)  # never keep a position that is unprotected or already through its stop
            raise
        return out
    if decision.action == "close":
        pos = position_from_ib(ib, tag, symbol)
        return reduce_position(executor, ib, tag, symbol, pos.shares if pos else 0, ref_price)
    return reduce_position(executor, ib, tag, symbol, shares, ref_price)
