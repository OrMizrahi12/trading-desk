"""Bars still being filled by IB are never decided on.

The paper account's data is 15 minutes delayed, so IB returns only bars that started 15 or more minutes ago and the newest of
them is still partial (measured on the desk's saved snapshots: a bar seen 12 minutes after its close held 21% of its final
volume; the newest bar of 63 of 79 snapshots later changed, the second newest never). The lab's feed keeps a bar as soon as
the wall clock says it is over, so without the settle step the desk decided on that partial bar.
"""

import json

import pandas as pd
import pytest

from conftest import WAIT, FakeFeed
from desk import snapshot
from desk.heartbeat import Heartbeat

BAR = pd.Timedelta(minutes=5)
DELAY = pd.Timedelta(minutes=15)


class DelayedFeed(FakeFeed):
    """Like IB with delayed data: only bars that started by now-15min exist, and the newest of them is partial."""

    def __init__(self, bars, clock):
        super().__init__(bars, len(bars))
        self.clock = clock

    def poll(self):
        now = self.clock()
        raw = self.bars[self.bars.index <= now - DELAY].astype(float)
        partial = raw.index + BAR > now - DELAY  # the bar IB is still filling
        raw.loc[partial, "volume"] *= 0.2
        raw.loc[partial, "close"] += 0.5
        return raw[raw.index + BAR <= now]  # the lab's closed_bars: the wall clock says the bar is over


def delayed(rig, *, at_bar, age_min, **kw):
    """A rig whose clock is `age_min` after the close of bar `at_bar`, on a delayed feed."""
    r = rig(shown=at_bar + 1, **kw)
    r.now = r.bars.index[at_bar] + BAR + pd.Timedelta(minutes=age_min)
    r.feed = r.hb.feed = DelayedFeed(r.bars, lambda: r.now)
    return r


def test_settled_bars_keeps_only_bars_older_than_the_delay():
    idx = pd.date_range("2026-10-21 14:00", periods=6, freq="5min", tz="UTC")
    df = pd.DataFrame({"close": range(6)}, index=idx)
    now = idx[5] + BAR  # the last bar ended just now
    assert list(snapshot.settled_bars(df, now, 16).index) == list(idx[:2])  # now 14:30: only bars that ended by 14:14 (14:05, 14:10) are final
    assert list(snapshot.settled_bars(df, idx[2] + BAR + pd.Timedelta(minutes=16), 16).index) == list(idx[:3])
    assert list(snapshot.settled_bars(df, idx[2] + BAR + pd.Timedelta(minutes=15.9), 16).index) == list(idx[:2])
    assert snapshot.settled_bars(df, now, 0) is df  # 0 = off


def test_production_default_waits_for_the_delay_plus_a_margin():
    import inspect
    assert snapshot.SETTLE_MIN == 16.0
    assert inspect.signature(Heartbeat.__init__).parameters["settle_min"].default == snapshot.SETTLE_MIN


def test_without_the_settle_step_the_delayed_feed_hands_over_a_partial_bar(rig):
    r = delayed(rig, at_bar=190, age_min=12, settle_min=0.0)
    last = r.feed.poll().iloc[-1]
    assert last["volume"] == pytest.approx(0.2 * r.bars["volume"].iloc[last.name == r.bars.index].iloc[0])  # the bug's input


def saved(r):
    """The full snapshot of every handled bar, from the journal (the model may have been sent only a delta)."""
    return [json.loads(open(rec["snapshot_file"]).read()) for rec in r.records()]


def test_the_newest_bar_ib_is_still_filling_is_skipped_and_taken_once_it_is_final(rig):
    r = delayed(rig, at_bar=190, age_min=12, settle_min=snapshot.SETTLE_MIN, answers=[WAIT])
    partial = r.bars.index[190]  # started at now-15min: IB shows it with 20% of its volume
    assert r.feed.poll().index[-1] == partial
    recs = r.hb.tick()
    assert [pd.Timestamp(x["bar"]) for x in recs] == [r.bars.index[189]]  # the finished bar before it, never the partial
    assert r.hb.tick() == []  # nothing new until `partial` is final
    r.now += pd.Timedelta(minutes=4)  # now - 16min is past the partial bar's end
    recs = r.hb.tick()
    assert [pd.Timestamp(x["bar"]) for x in recs] == [partial]
    seen = saved(r)[-1]["bars_5min"][-1]
    final = r.bars.loc[partial]
    assert seen["close"] == pytest.approx(final["close"], abs=1e-3) and seen["volume"] == final["volume"]


def test_every_bar_the_model_sees_is_final_over_a_whole_stretch_of_polls(rig):
    r = delayed(rig, at_bar=150, age_min=12, settle_min=snapshot.SETTLE_MIN, answers=[WAIT])
    for _ in range(40):  # a poll every minute
        r.hb.tick()
        r.now += pd.Timedelta(minutes=1)
    snaps = saved(r)
    assert len(snaps) >= 6
    for snap in snaps:
        for row in snap["bars_5min"][-5:]:
            final = r.bars.loc[pd.Timestamp(row["start"]).tz_convert("UTC")]
            assert row["volume"] == final["volume"] and row["close"] == pytest.approx(final["close"], abs=1e-3)
    ages = [s["session"]["bar_age_minutes"] for s in snaps]
    assert min(ages) >= snapshot.SETTLE_MIN and max(ages) < 30  # later than before, still inside the stale-data limit


def test_the_same_run_without_the_settle_step_decides_on_partial_bars(rig):
    r = delayed(rig, at_bar=150, age_min=12, settle_min=0.0, answers=[WAIT])
    for _ in range(40):
        r.hb.tick()
        r.now += pd.Timedelta(minutes=1)
    wrong = 0
    for snap in saved(r):
        row = snap["bars_5min"][-1]
        wrong += row["volume"] != r.bars.loc[pd.Timestamp(row["start"]).tz_convert("UTC")]["volume"]
    assert wrong >= 1  # the bug: the newest bar of a snapshot was not yet what IB finally reported


def test_the_flatten_seatbelt_still_sees_the_newest_price_even_from_a_bar_being_filled(rig):
    r = delayed(rig, at_bar=190, age_min=12, settle_min=snapshot.SETTLE_MIN)
    r.hb.tick()
    assert r.hb._last_close == pytest.approx(r.bars["close"].iloc[190] + 0.5)  # the partial bar's close: the freshest price


def test_the_mcp_trade_path_also_waits_for_a_finished_bar():
    from datetime import datetime  # noqa: F401
    from types import SimpleNamespace
    from desk.mcp_server.core import Desk, Refused
    idx = pd.date_range("2026-10-21 14:00", periods=4, freq="5min", tz="UTC")
    raw = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx)
    desk = Desk.__new__(Desk)
    desk.settle_min, desk.clock = snapshot.SETTLE_MIN, lambda: idx[3] + BAR + pd.Timedelta(minutes=12)
    desk.parts = lambda ib: SimpleNamespace(feed=SimpleNamespace(poll=lambda: raw))
    m = Desk.market(desk, None)
    assert m.bar_start == idx[2]  # the 14:15 bar ended at 14:20, 12 min before now; only bars ending by 14:16 are final: newest = start 14:10
