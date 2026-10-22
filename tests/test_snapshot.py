"""The snapshot: all sections present, 15-minute and daily bars built right, no look-ahead."""

import pandas as pd

from conftest import make_bars
from desk import snapshot


def test_bars_15min_are_built_from_three_five_minute_bars_and_incomplete_ones_dropped():
    five = make_bars(days=("2026-10-21",)).iloc[:8]  # 09:30..10:05: two full 15-minute bars + a partial one
    fifteen = snapshot.bars_15min(five)
    assert len(fifteen) == 2
    first = five.iloc[:3]
    assert fifteen.iloc[0]["open"] == first["open"].iloc[0] and fifteen.iloc[0]["close"] == first["close"].iloc[2]
    assert fifteen.iloc[0]["high"] == first["high"].max() and fifteen.iloc[0]["volume"] == first["volume"].sum()


def test_daily_bars_group_by_new_york_session_date():
    daily = snapshot.daily_from_5min(make_bars())
    assert [str(d.date()) for d in daily.index] == ["2026-10-19", "2026-10-20", "2026-10-21"]


def test_build_has_every_section(rig):
    r = rig()
    r.hb.tick()
    snap = r.model.calls[0]
    for key in ("session", "bars_5min", "bars_15min", "bars_daily", "positions", "open_orders", "account",
                "stop_file_present", "risk_limits", "recent_journal", "todays_fills"):
        assert key in snap
    assert snap["bars_5min"][-1]["ema9"] is not None and "vwap" in snap["bars_5min"][-1]
    assert snap["bars_5min"][0]["start"].endswith("-04:00")  # New York time
    assert snap["account"]["NetLiquidation"] == 100_000.0
    assert snap["session"]["bar_age_minutes"] == 7.0 and snap["session"]["is_newest_closed_bar"]


def test_no_look_ahead(rig):
    r = rig()
    closed = r.feed.poll()
    older = closed.index[-10]
    snap = snapshot.build(closed=closed, bar_start=older, ib=r.ib, tag="live:desk:SPY", symbol="SPY", now=r.now,
                          daily_history=None, journal_tail=[], stop_file=False, limits={})
    assert pd.Timestamp(snap["bars_5min"][-1]["start"]).tz_convert("UTC") == older
    assert not snap["session"]["is_newest_closed_bar"]
