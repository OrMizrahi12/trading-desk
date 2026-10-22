"""Seatbelts: hard safety rules that live in code and always win over the model. Paper trading only.

The model decides every trade. A seatbelt is not Python trading; it is Python enforcing the desk's rules, and each
time one acts it is logged and written to the journal (`seatbelt` / `seatbelts` fields). The seatbelts:

- stop_file      the lab's kill switch file exists: no new buys.
- risk_limits    the lab's per-symbol / per-day limits refuse or shrink a buy.
- entry_window   no new buys on old bars, outside the session, or in the last minutes before the close.
- flatten_before_close  at (session close - FLATTEN_BEFORE_CLOSE_MIN minutes) by the wall clock, any long
                 position is sold. Wall clock, not bars: bars arrive 15-20 minutes late.
- overnight_carry  a position the desk bought on an earlier day is sold when the market opens. Never overnight.
- limits_pin    the lab's risk limits file no longer matches its committed fingerprint (desk/limits_pin.py): no new buys.
- mcp_active    the MCP server (desk/mcp_server) has orders on the symbol: the heartbeat trades nothing and sells
                 nothing alongside it (the server flattens its own position, 5 minutes before the heartbeat would).
- paper_guard    a non-paper port, host or account stops everything.
"""

import pandas as pd

from . import lab  # noqa: F401
from live.config import FLATTEN_BEFORE_CLOSE_MIN

from .snapshot import _CAL, NY


def flatten_reason(now: pd.Timestamp, entry_day: str | None) -> str | None:
    """Why a long position must be sold right now, or None. `entry_day` = New York date the desk bought it."""
    day = pd.Timestamp(now.tz_convert(NY).date())
    if not _CAL.is_session(day):
        return None
    if not _CAL.session_open(day) <= now < _CAL.session_close(day):
        return None  # closed: nothing can be sold now (the caller reports it)
    if now >= _CAL.session_close(day) - pd.Timedelta(minutes=FLATTEN_BEFORE_CLOSE_MIN):
        return "flatten_before_close"
    if entry_day is not None and entry_day != str(day.date()):
        return "overnight_carry"
    return None


def market_closed_with_position(now: pd.Timestamp) -> bool:
    """True when the exchange is shut right now (so a position cannot be sold until it opens)."""
    day = pd.Timestamp(now.tz_convert(NY).date())
    return not (_CAL.is_session(day) and _CAL.session_open(day) <= now < _CAL.session_close(day))
