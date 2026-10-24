"""The snapshot: everything the model gets to see for one closed bar. Facts only, no signals, no filtering.

Terms:
- bar: a summary of one time slice of trading: open, high, low, close price and volume. Bars are stamped with their
  START time. A 5-minute bar is "closed" once its 5 minutes are over.
- indicator: a number computed from past bars. ema = exponential moving average (a smoothed price, newer bars weigh
  more), rsi = relative strength index (0-100 momentum gauge), atr = average true range (typical bar size in
  dollars), bollinger = a band two standard deviations around a 20-bar average, vwap = volume-weighted average
  price since the session's open.
- net liquidation: the account's total value, cash plus positions.
"""

import math

import pandas as pd

from . import lab  # noqa: F401  (puts the trading lab on sys.path)
import exchange_calendars as xcals

import indicators
from live.config import BAR_MINUTES, FLATTEN_BEFORE_CLOSE_MIN, MAX_SIGNAL_AGE_MIN
from live.refs import owns
from market_data import resample_bars

NY = "America/New_York"
BAR = pd.Timedelta(minutes=BAR_MINUTES)
ROWS = {"5min": 100, "15min": 60, "daily": 30}  # how many of the latest bars of each size the model sees
_CAL = xcals.get_calendar("XNYS")


def _clean(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "item"):
        return _clean(value.item())
    return value


def _rows(df: pd.DataFrame, n: int, daily: bool = False) -> list[dict]:
    out = []
    for ts, row in df.tail(n).iterrows():
        rec = {"start": ts.strftime("%Y-%m-%d") if daily else ts.tz_convert(NY).isoformat()}
        rec.update({k: _clean(round(float(v), 4)) if v == v else None for k, v in row.items()})
        out.append(rec)
    return out


def _nan(df: pd.DataFrame) -> pd.Series:
    return pd.Series(float("nan"), index=df.index)


def _try(fn, df: pd.DataFrame, *cols_and_args):
    """An indicator, or an all-empty column when there are too few bars for it (the lab's functions raise then)."""
    try:
        return fn(*cols_and_args)
    except (IndexError, ValueError):
        return _nan(df)


def add_indicators(df: pd.DataFrame, *, intraday: bool) -> pd.DataFrame:
    """Add ema9, ema21, rsi14, atr14, the 20-bar Bollinger bands and (intraday only) the session VWAP.

    An indicator that needs more bars than exist is left empty (null), never guessed.
    """
    out = df.copy()
    out["ema9"], out["ema21"] = _try(indicators.ema, df, df["close"], 9), _try(indicators.ema, df, df["close"], 21)
    out["rsi14"] = _try(indicators.rsi, df, df["close"], 14)
    out["atr14"] = _try(indicators.atr, df, df["high"], df["low"], df["close"], 14)
    try:
        band = indicators.bollinger(df["close"], 20, 2.0)
        out["bb_lower"], out["bb_upper"] = band["lower"], band["upper"]
    except (IndexError, ValueError):
        out["bb_lower"] = out["bb_upper"] = _nan(df)
    if intraday:
        out["vwap"] = _try(indicators.vwap, df, df["high"], df["low"], df["close"], df["volume"])
    return out


def bars_15min(five: pd.DataFrame) -> pd.DataFrame:
    """15-minute bars from 5-minute ones. A 15-minute bar still missing one of its three 5-minute bars is dropped."""
    parts = five[["open", "high", "low", "close", "volume"]].copy()
    parts["n"] = 1
    both = parts.resample("15min").agg({"open": "first", "high": "max", "low": "min", "close": "last",
                                        "volume": "sum", "n": "sum"}).dropna(subset=["open"])
    return both[both["n"] == 3].drop(columns="n")


def daily_from_5min(five: pd.DataFrame) -> pd.DataFrame:
    """One bar per New York session date, built from 5-minute bars."""
    local = five.copy()
    local["day"] = local.index.tz_convert(NY).date
    daily = local.groupby("day").agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                                     close=("close", "last"), volume=("volume", "sum"))
    daily.index = pd.DatetimeIndex(daily.index)
    return daily


def merge_daily(history: pd.DataFrame | None, five: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    """Completed days from `history` (IB daily bars) plus the days seen in `five`; the 5-minute data wins on overlap."""
    derived = daily_from_5min(five)
    if history is None or history.empty:
        return derived, "derived from the 5-minute bars (about a week)"
    hist = history[["open", "high", "low", "close", "volume"]]
    hist = hist[~hist.index.isin(derived.index)]
    return pd.concat([hist, derived]).sort_index(), "IB daily bars plus today's partial day"


def session_info(now: pd.Timestamp, bar_start: pd.Timestamp, newest_start: pd.Timestamp) -> dict:
    local = now.tz_convert(NY)
    day = pd.Timestamp(local.date())
    info = {"now": local.isoformat(), "date": str(local.date()), "is_session": bool(_CAL.is_session(day))}
    if info["is_session"]:
        opens, closes = _CAL.session_open(day), _CAL.session_close(day)
        info.update(open=opens.tz_convert(NY).isoformat(), close=closes.tz_convert(NY).isoformat(),
                    market_open_now=bool(opens <= now < closes),
                    minutes_to_close=round((closes - now).total_seconds() / 60, 1),
                    half_day=closes.tz_convert(NY).hour < 16)
    bar_close = bar_start + BAR
    info.update(bar_start=bar_start.tz_convert(NY).isoformat(), bar_close=bar_close.tz_convert(NY).isoformat(),
                bar_age_minutes=round((now - bar_close).total_seconds() / 60, 1),
                is_newest_closed_bar=bool(bar_start == newest_start),
                max_entry_age_minutes=MAX_SIGNAL_AGE_MIN, no_new_entries_minutes_before_close=FLATTEN_BEFORE_CLOSE_MIN,
                flatten_minutes_before_close=FLATTEN_BEFORE_CLOSE_MIN)
    return info


def account_info(ib) -> dict:
    out = {}
    try:
        account = ib.managedAccounts()[0]
        for row in ib.accountSummary(account):
            try:
                out[row.tag] = float(row.value)
            except (TypeError, ValueError):
                out[row.tag] = row.value
        out["account"] = account
    except Exception as exc:  # the model still sees the bar; it is told the account could not be read
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def positions_info(ib) -> list[dict]:
    return [{"symbol": p.contract.symbol, "shares": float(p.position), "avg_cost": _clean(float(p.avgCost))}
            for p in ib.positions() if p.position]


def _price(value) -> float | None:
    """An order price, or None when unset (ib_async marks 'unset' with a huge number, 0 also means none)."""
    return None if not value or value > 1e300 else round(float(value), 4)


def orders_info(ib, tag: str) -> list[dict]:
    ib.reqAllOpenOrders()
    return [{"symbol": t.contract.symbol, "action": t.order.action, "type": t.order.orderType,
             "shares": float(t.order.totalQuantity), "limit": _price(t.order.lmtPrice),
             "stop": _price(t.order.auxPrice), "status": t.orderStatus.status,
             "ours": owns(tag, t.order.orderRef or "")} for t in ib.openTrades()]


def todays_fills(ib) -> list[dict]:
    try:
        return [{"time": f.execution.time.isoformat(), "side": f.execution.side, "shares": float(f.execution.shares),
                 "price": float(f.execution.price)} for f in ib.fills()][-10:]
    except Exception:
        return []


def build(*, closed: pd.DataFrame, bar_start: pd.Timestamp, ib, tag: str, symbol: str, now: pd.Timestamp,
          daily_history: pd.DataFrame | None, journal_tail: list[dict], stop_file: bool, limits: dict,
          review_block: str | None = None) -> dict:
    """The snapshot for the bar starting at `bar_start`. Only bars closed by then are included (no look-ahead)."""
    five = closed[closed.index <= bar_start]
    daily, daily_source = merge_daily(daily_history, five)
    return {
        "symbol": symbol,
        "session": session_info(now, bar_start, closed.index[-1]),
        "bars_5min": _rows(add_indicators(five, intraday=True), ROWS["5min"]),
        "bars_15min": _rows(add_indicators(bars_15min(five), intraday=True), ROWS["15min"]),
        "bars_daily": _rows(add_indicators(daily, intraday=False), ROWS["daily"], daily=True),
        "daily_source": daily_source,
        "positions": positions_info(ib),
        "open_orders": orders_info(ib, tag),
        "todays_fills": todays_fills(ib),
        "account": account_info(ib),
        "stop_file_present": stop_file,
        "risk_limits": limits,
        "recent_journal": journal_tail,
        # post-trade review = a written look back at a finished trade; until the last closed trade has one, entries are refused
        "entry_review_gate": {"entries_open": review_block is None, "why_closed": review_block},
    }


DELTA_NOTE = ("Only what changed since your last bar. Earlier 5-minute, 15-minute and daily bars and your own earlier "
              "answers are in this conversation above. Positions, orders, fills, account and seatbelt status are current.")


def delta(snap: dict, prev_bar_start: str) -> dict | None:
    """What a resumed session needs for the bar in `snap`: the part that changed since the bar starting `prev_bar_start`.

    New 5-minute bars (every one after the previous bar, so a skipped bar is not lost) and new 15-minute bars with their
    indicators, today's daily bar (it changes on every bar), and the live state: session clock, positions, open orders,
    fills, account, STOP-file flag, limits, and the journal entries the session has not seen (the desk's verdict and
    the result of the last decision). None when the previous bar is older than the history in `snap`: send it all.
    """
    prev = pd.Timestamp(prev_bar_start)
    rows5 = snap["bars_5min"]
    if not rows5 or pd.Timestamp(rows5[0]["start"]) > prev.tz_convert(NY):
        return None
    after = lambda rows, cutoff: [r for r in rows if pd.Timestamp(r["start"]) > cutoff]  # noqa: E731
    out = {"snapshot_kind": "delta", "note": DELTA_NOTE, "symbol": snap["symbol"], "session": snap["session"],
           "new_bars_5min": after(rows5, prev.tz_convert(NY)),
           # a 15-minute bar is new once it completed after the previous bar closed: start + 15 min > prev start + 5 min
           "new_bars_15min": after(snap["bars_15min"], prev.tz_convert(NY) - pd.Timedelta(minutes=10)),
           "today_daily_bar": snap["bars_daily"][-1:]}
    for k in ("positions", "open_orders", "todays_fills", "account", "stop_file_present", "risk_limits", "entry_review_gate"):
        out[k] = snap[k]
    out["recent_journal"] = [j for j in snap["recent_journal"] if j.get("bar") and pd.Timestamp(j["bar"]) >= prev]
    return out


def fetch_ib_daily(ib, contract) -> pd.DataFrame:
    """About 30 calendar days of IB daily bars (one throttled history request). Index = session date."""
    from ib_async import util
    from market_data import hist_throttle

    hist_throttle.wait(ib, (contract.conId, "TRADES", True, "1 day"))
    bars = ib.reqHistoricalData(contract, endDateTime="", durationStr="30 D", barSizeSetting="1 day",
                                whatToShow="TRADES", useRTH=True, formatDate=2)
    df = util.df(bars)
    df = df.set_index(pd.DatetimeIndex(pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize()))
    return df[["open", "high", "low", "close", "volume"]]
