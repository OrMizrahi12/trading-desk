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
from datetime import datetime

from . import lab  # noqa: F401
from live.execution import ExecutionHalt
from live.reconcile import ib_shares, open_orders
from live.refs import entry_ref, owns
from live.state import PositionState

from .decision import Decision
from .journal import NY, Journal

log = logging.getLogger("desk")


MCP_TAG_PREFIX = "live:mcp:"  # orderRef prefix of the MCP server's orders (desk/mcp_server), e.g. live:mcp:SPY


class ReviewPending(RuntimeError):
    """A new entry was refused: the last closed trade has no post-trade review yet. The message says why, for the trader."""


def review_block(journal: Journal) -> str | None:
    """Why no new entry may be made now, or None. Post-trade review = a written look back at a finished trade (what
    happened, whether the decision was sound, one lesson); the desk's note writer makes it after every closed trade, and
    until it is in the journal the desk takes no new entry. Exits are never held back."""
    t = journal.pending_review()
    if t is None:
        return None
    start = datetime.fromisoformat(t["entry_bar"]).astimezone(NY)
    ended = t.get("exit_type") or "exit not recorded"
    result = f", result {t['result_r']}R" if t.get("result_r") is not None else ""
    return (f"no new entry until the last closed trade has its post-trade review: the {start:%H:%M} New York trade of "
            f"{t['day']} ended ({ended}{result}) and the desk's note writer has not yet written its review. Entries "
            f"reopen on their own once it is written; selling is still allowed.")


def mcp_orders(ib, symbol: str) -> list:
    """Open orders on `symbol` that the MCP server placed. While any rests, the MCP server holds or is about to hold
    the symbol, and the heartbeat must not trade it: IB lets only the placing client change an order, so selling the
    MCP server's shares from here would leave its stop live."""
    tag = MCP_TAG_PREFIX + symbol
    return [t for t in open_orders(ib, symbol) if not t.isDone() and owns(tag, t.order.orderRef or "")]


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


def run(decision: Decision, shares: int, *, executor, ib, tag: str, symbol: str, bar_ts, ref_price: float, state,
        journal: Journal | None = None) -> dict:
    """Execute a buy / sell / close. Raises ReviewPending, ExecutionHalt, OrderRejected, RiskRefused, DuplicateEntry for
    the caller. `journal` is the desk's journal: a buy is refused while its last closed trade has no review (None = not
    checked, for callers that keep no trade journal)."""
    if decision.action == "buy":
        if journal is not None and (why := review_block(journal)):
            raise ReviewPending(why)
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
