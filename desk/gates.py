"""Gates between the model's decision and the broker. Python may REFUSE an unsafe decision; it never makes one.

Terms:
- STOP file: the lab's kill switch. If the file exists the lab refuses every new buy. Exits (sells) still pass, as
  in the lab, so a position can always be reduced. The desk never deletes the file.
- risk limits: the lab's per-symbol maximum shares / position value, trades per day and daily loss, checked again
  by the lab's broker when the order is sent.
- no leverage: never buy more than the account can pay for in cash.
"""

import math
from dataclasses import dataclass

from . import lab  # noqa: F401
from backtests.sizing import RISK_PCT
from live.config import FLATTEN_BEFORE_CLOSE_MIN, LIMIT_OFFSET_PCT, MAX_SIGNAL_AGE_MIN
from live.risk import stop_path

from .decision import Decision, InvalidDecision, check_prices


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str = ""
    shares: int = 0
    seatbelt: str | None = None  # set when a seatbelt (see seatbelts.py) refused or shrank this decision
    note: str = ""


def shares_for_r(size_r: float, ref_price: float, stop: float, net_liq: float, *,
                 offset_pct: float = LIMIT_OFFSET_PCT, risk_pct: float = RISK_PCT) -> int:
    """Shares worth `size_r` times R. 1R = `risk_pct` of net liquidation; risk per share = worst fill - stop."""
    fill = ref_price * (1 + offset_pct)  # the marketable limit may fill this high
    risk_ps = fill - stop
    if risk_ps <= 0 or net_liq <= 0:
        return 0
    shares = math.floor(size_r * net_liq * risk_pct / risk_ps)
    return max(0, min(shares, math.floor(net_liq / fill)))  # no leverage


def stop_file_present(root=None) -> bool:
    """Does the lab's kill switch file exist? `root` is the lab's state folder (default: the lab's own)."""
    return (stop_path(root) if root else stop_path()).exists()


def gate(decision: Decision, *, ref_price: float, held: int, resting_buys: int, session: dict, net_liq: float | None,
         position_stop: float | None, risk, ib, symbol: str, stop_file: bool, review_block: str | None = None) -> Verdict:
    """Allow or refuse `decision`. `held` = shares long now; `session` = the snapshot's session block.
    `review_block` = why the last closed trade's post-trade review still holds back new entries (see execute.review_block)."""
    action = decision.action
    if action == "wait":
        return Verdict(True, "wait")
    if action == "close":
        return Verdict(True, "close") if held > 0 else Verdict(False, "nothing to close: no long position")
    if net_liq is None:
        return Verdict(False, "account value unreadable")
    if action == "sell":
        if held <= 0:
            return Verdict(False, "nothing to sell: no long position (the desk never goes short)")
        if not position_stop:
            return Verdict(False, "cannot size a sell in R: the position has no stop at the broker")
        n = shares_for_r(decision.size_r, ref_price, position_stop, net_liq)
        return Verdict(True, "sell", min(held, max(1, n)))
    # buy
    if stop_file:
        return Verdict(False, "STOP file present: no new buys", seatbelt="stop_file")
    if not session.get("is_newest_closed_bar", False):
        return Verdict(False, "superseded: a newer closed bar already exists")
    if session["bar_age_minutes"] > MAX_SIGNAL_AGE_MIN:
        return Verdict(False, f"bar is {session['bar_age_minutes']} min old, entries allowed up to {MAX_SIGNAL_AGE_MIN}",
                       seatbelt="entry_window")
    if not session.get("market_open_now"):
        return Verdict(False, "market is closed", seatbelt="entry_window")
    if session["minutes_to_close"] <= FLATTEN_BEFORE_CLOSE_MIN:
        return Verdict(False, f"within {FLATTEN_BEFORE_CLOSE_MIN} min of the close: no new entries",
                       seatbelt="entry_window")
    if review_block:
        return Verdict(False, review_block, seatbelt="post_trade_review")
    if held > 0 or resting_buys > 0:
        return Verdict(False, "already long or a buy is resting: no adding to a position")
    try:
        check_prices(decision, ref_price)
    except InvalidDecision as exc:
        return Verdict(False, f"invalid prices: {exc}")
    wanted = shares_for_r(decision.size_r, ref_price, decision.stop, net_liq)
    shares = risk.cap_shares(ib, symbol, wanted, round(ref_price * (1 + LIMIT_OFFSET_PCT), 2)) if wanted > 0 else 0
    if wanted > 0 and shares <= 0:
        return Verdict(False, "no room under the lab's risk limits", seatbelt="risk_limits")
    if shares <= 0:
        return Verdict(False, "size is 0 shares (stop too close to the price)")
    if shares < wanted:
        return Verdict(True, "buy", shares, seatbelt="risk_limits", note=f"size cut from {wanted} to {shares} shares")
    return Verdict(True, "buy", shares)
