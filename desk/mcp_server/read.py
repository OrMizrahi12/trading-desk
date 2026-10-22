"""The read-only tools' logic. Each function is `f(ib, desk, **args)` and runs on the IB thread. None places an order.

Every price here comes from a DELAYED feed (see core.DELAY_NOTE).
"""

import math
import re

import pandas as pd
from ib_async import util

from .. import snapshot
from .core import DELAY_NOTE, SYMBOL, Desk, Refused, owner_of
from live.reconcile import ib_shares
from market_data import hist_throttle

BAR_SIZES = ["1 secs", "5 secs", "10 secs", "15 secs", "30 secs", "1 min", "2 mins", "3 mins", "5 mins", "10 mins",
             "15 mins", "20 mins", "30 mins", "1 hour", "2 hours", "3 hours", "4 hours", "8 hours", "1 day",
             "1 week", "1 month"]
WHAT_TO_SHOW = ("TRADES", "MIDPOINT", "BID", "ASK")
_UNIT_SECONDS = {"secs": 1, "min": 60, "mins": 60, "hour": 3600, "hours": 3600}
_DURATION = re.compile(r"^(\d{1,5}) (S|D|W|M|Y)$")
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
MAX_ROWS = 500

GLOSSARY = {
    "ema9 / ema21": "exponential moving averages: smoothed prices over about 9 / 21 bars where newer bars weigh more; price above both = uptrend",
    "rsi14": "relative strength index over 14 bars, a 0-100 momentum gauge; low = beaten down, high = stretched",
    "atr14": "average true range over 14 bars: the typical bar size in dollars",
    "bb_lower / bb_upper": "Bollinger bands: two standard deviations around a 20-bar average; touching an edge = stretched",
    "vwap": "volume-weighted average price since the session's open (only for intraday bars)",
}


def bar_seconds(bar_size: str) -> int | None:
    """Length of an intraday bar in seconds, None for day / week / month bars."""
    n, unit = bar_size.split()
    return int(n) * _UNIT_SECONDS[unit] if unit in _UNIT_SECONDS else None


def _check_symbol(symbol: str) -> str:
    symbol = symbol.strip().upper()
    if not _SYMBOL.match(symbol):
        raise Refused(f"{symbol!r} is not a plain US stock symbol")
    return symbol


def fetch_bars(ib, desk: Desk, symbol: str, bar_size: str, duration: str, what_to_show: str = "TRADES",
               use_rth: bool = True) -> tuple[pd.DataFrame, bool]:
    """Closed bars as a frame of open/high/low/close/volume. Returns (frame, is_intraday).

    The index is UTC for intraday bars and the session date for day / week / month bars. The bar still forming is
    dropped for intraday sizes, so every row is a finished bar.
    """
    symbol = _check_symbol(symbol)
    if bar_size not in BAR_SIZES:
        raise Refused(f"bar_size {bar_size!r} is not one IB offers; use one of {BAR_SIZES}")
    if not _DURATION.match(duration):
        raise Refused(f"duration {duration!r} must look like '2 D' (number, space, then S, D, W, M or Y)")
    if what_to_show not in WHAT_TO_SHOW:
        raise Refused(f"what_to_show must be one of {WHAT_TO_SHOW}")
    contract = desk.contract(ib, symbol)
    hist_throttle.wait(ib, (contract.conId, what_to_show, use_rth, bar_size, duration))  # IB's pacing rules
    errors: list[tuple[int, str]] = []
    handler = lambda reqId, code, text, c=None: errors.append((code, text))  # noqa: E731
    ib.errorEvent += handler
    try:
        bars = ib.reqHistoricalData(contract, endDateTime="", durationStr=duration, barSizeSetting=bar_size,
                                    whatToShow=what_to_show, useRTH=use_rth, formatDate=2)
    finally:
        ib.errorEvent -= handler
    seconds = bar_seconds(bar_size)
    if not bars:
        hard = [f"[{c}] {t}" for c, t in errors if c < 2100 or c >= 2200]
        raise Refused("IB sent no bars" + (f": {hard[-1]}" if hard else " (no trading in that range, or no permission for it)"))
    df = util.df(bars)
    if seconds is None:
        df.index = pd.DatetimeIndex(pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize())
    else:
        df.index = pd.DatetimeIndex(pd.to_datetime(df["date"], utc=True))
        df = df[df.index + pd.Timedelta(seconds=seconds) <= desk.clock()]
    return df[["open", "high", "low", "close", "volume"]], seconds is not None


def _freshness(df: pd.DataFrame, bar_size: str, now: pd.Timestamp) -> dict:
    seconds = bar_seconds(bar_size)
    if seconds is None or df.empty:
        return {}
    closed_at = df.index[-1] + pd.Timedelta(seconds=seconds)
    return {"newest_bar_closed_at": closed_at.tz_convert(snapshot.NY).isoformat(),
            "newest_bar_age_minutes": round((now - closed_at).total_seconds() / 60, 1)}


def get_bars(ib, desk: Desk, symbol: str = SYMBOL, bar_size: str = "5 mins", duration: str = "1 D",
             what_to_show: str = "TRADES", use_rth: bool = True, max_rows: int = 100) -> dict:
    df, intraday = fetch_bars(ib, desk, symbol, bar_size, duration, what_to_show, use_rth)
    n = max(1, min(int(max_rows), MAX_ROWS))
    return {"symbol": symbol.upper(), "bar_size": bar_size, "duration": duration, "what_to_show": what_to_show,
            "regular_hours_only": use_rth, "bars_available": len(df), "bars_returned": min(n, len(df)),
            "truncated": len(df) > n, "bars": snapshot._rows(df, n, daily=not intraday), "delayed": True,
            **_freshness(df, bar_size, desk.clock()),
            "note": DELAY_NOTE + " Oldest bar first; times are New York; the bar still forming is left out."
                    + ("" if intraday else " A day/week/month bar for the current period may include a partial period.")}


def get_indicators(ib, desk: Desk, symbol: str = SYMBOL, bar_size: str = "5 mins", duration: str = "2 D",
                   rows: int = 5) -> dict:
    df, intraday = fetch_bars(ib, desk, symbol, bar_size, duration)
    with_ind = snapshot.add_indicators(df, intraday=intraday)
    n = max(1, min(int(rows), 100))
    return {"symbol": symbol.upper(), "bar_size": bar_size, "bars_used": len(df), "rows": snapshot._rows(with_ind, n, daily=not intraday),
            "glossary": GLOSSARY, "delayed": True, **_freshness(df, bar_size, desk.clock()),
            "note": DELAY_NOTE + " Computed with the lab's indicators module. An indicator is null when there are too "
                    "few bars for it (ema21 needs 21, bollinger 20, rsi14/atr14 15)."}


def _finite(x):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) or x == -1 else float(x)


def get_quote(ib, desk: Desk, symbol: str = SYMBOL, wait_s: float = 5.0) -> dict:
    symbol = _check_symbol(symbol)
    contract = desk.contract(ib, symbol)
    ticker = ib.reqMktData(contract, "", True, False)  # snapshot: one delayed reading, no streaming line left open
    try:
        waited = 0.0
        while waited < wait_s and _finite(ticker.last) is None and _finite(ticker.bid) is None and _finite(ticker.close) is None:
            ib.sleep(0.25)
            waited += 0.25
    finally:
        ib.cancelMktData(contract)
    quote = {k: _finite(getattr(ticker, k, None)) for k in ("bid", "ask", "last", "close", "volume")}
    if any(v is not None for k, v in quote.items() if k != "volume"):
        stamp = getattr(ticker, "time", None)
        return {"symbol": symbol, "source": "delayed_quote", **quote, "quote_time": stamp.isoformat() if stamp else None,
                "delayed": True, "note": DELAY_NOTE + " `close` is the previous session's close; bid = best price a buyer "
                "offers, ask = best price a seller asks, last = latest trade."}
    df, _ = fetch_bars(ib, desk, symbol, "5 mins", "1 D")  # no quote arrived: the newest closed 5-minute bar instead
    last = df.iloc[-1]
    return {"symbol": symbol, "source": "last_closed_5min_bar", "bid": None, "ask": None, "last": float(last["close"]),
            "close": None, "volume": float(last["volume"]), "delayed": True, **_freshness(df, "5 mins", desk.clock()),
            "note": DELAY_NOTE + " No delayed quote arrived, so `last` is the close of the newest closed 5-minute bar."}


def get_positions(ib, desk: Desk) -> dict:
    rows = snapshot.positions_info(ib)
    own = desk.ownership(ib)
    for r in rows:
        r["owner"] = ("mcp" if own.state == "mine" else "heartbeat or other") if r["symbol"] == SYMBOL else "other"
        r["manageable_here"] = r["symbol"] == SYMBOL and own.state == "mine"
    return {"positions": rows, "note": "Long-only paper positions as IB reports them (live, not delayed). `avg_cost` is the "
            "average price paid per share. Only a position whose stop/target this server placed can be managed here."}


def get_open_orders(ib, desk: Desk) -> dict:
    ib.reqAllOpenOrders()
    rows = []
    for t in ib.openTrades():
        owner = owner_of(t.order.orderRef)
        rows.append({"order_id": t.order.orderId, "symbol": t.contract.symbol,
                     "action": t.order.action, "type": t.order.orderType, "shares": float(t.order.totalQuantity),
                     "limit": snapshot._price(t.order.lmtPrice), "stop": snapshot._price(t.order.auxPrice),
                     "status": t.orderStatus.status, "order_ref": t.order.orderRef, "owner": owner,
                     "manageable_here": owner == "mcp"})
    return {"orders": rows, "note": "A stop is a resting order that sells at your loss limit; a target (limit sell) sells at "
            "your profit goal. IB lets only the placing client change an order, so `manageable_here` is true only for this "
            "server's own orders (owner 'mcp'); the heartbeat's are owner 'heartbeat'."}


_TAGS = ("NetLiquidation", "TotalCashValue", "BuyingPower", "AvailableFunds", "GrossPositionValue", "UnrealizedPnL",
         "RealizedPnL")


def get_account_summary(ib, desk: Desk) -> dict:
    info = snapshot.account_info(ib)
    if "error" in info:
        raise Refused(f"account unreadable: {info['error']}")
    account = info["account"]
    net = info.get("NetLiquidation")
    out = {"account": account, "paper_account": account.startswith("DU"), **{t: info.get(t) for t in _TAGS if t in info}}
    if isinstance(net, float):
        out["one_R_dollars"] = round(net * 0.01, 2)
    out["held_shares"] = {SYMBOL: ib_shares(ib, SYMBOL)}
    out["note"] = ("Paper money only. NetLiquidation = net liquidation, the total account value (cash plus positions). "
                   "1R = 1% of it: the most a trade should lose if its stop is hit. Account values are not market-data "
                   "delayed, but position values inside them use delayed prices.")
    return out
