"""The regime gate: the label, the safety wakes, and the heartbeat with the gate on and off. Fake model, no IB."""

import json

import pandas as pd
import pytest

from conftest import WAIT, make_bars
from desk import regime
from desk.regime import RANGING, TRENDING, UNKNOWN, decide

NY = "America/New_York"


def snap(*, move=0.0, atr=1.0, positions=(), orders=(), fills=(), mtc=200.0, n_today=10, yesterday_open=500.0):
    """A minimal snapshot: yesterday's bars, then `n_today` bars today; the last close is `move` from today's first open."""
    day0, day1 = pd.Timestamp("2026-10-20 09:30", tz=NY), pd.Timestamp("2026-10-21 09:30", tz=NY)
    rows = [{"start": (day0 + pd.Timedelta(minutes=5 * i)).isoformat(), "open": yesterday_open, "close": yesterday_open,
             "atr14": atr} for i in range(3)]
    rows += [{"start": (day1 + pd.Timedelta(minutes=5 * i)).isoformat(), "open": 600.0, "close": 600.0, "atr14": atr}
             for i in range(n_today)]
    rows[-1]["close"] = 600.0 + move
    return {"bars_5min": rows, "positions": list(positions), "open_orders": list(orders), "todays_fills": list(fills),
            "session": {"minutes_to_close": mtc}}


# -- the label -------------------------------------------------------------------------------

def test_distance_is_measured_from_todays_first_open_not_yesterdays():
    assert regime.distance_atr(snap(move=3.0, atr=1.0, yesterday_open=100.0)) == pytest.approx(3.0)


@pytest.mark.parametrize("move, label", [(5.0, RANGING), (5.01, TRENDING), (-5.5, TRENDING), (0.0, RANGING)])
def test_trending_only_beyond_five_atr_in_either_direction(move, label):
    assert regime.classify(snap(move=move))[0] == label


@pytest.mark.parametrize("bad", [dict(atr=0.0), dict(atr=None)])
def test_unknown_when_the_atr_is_missing(bad):
    assert regime.classify(snap(**bad)) == (UNKNOWN, None)


def test_no_look_ahead_only_the_last_row_and_earlier_ones_count():
    s = snap(move=0.0)
    before = regime.distance_atr(s)
    s["bars_5min"].append({**s["bars_5min"][-1], "start": "2026-10-21T10:30:00-04:00", "close": 900.0})
    assert before == 0.0 and regime.distance_atr(s) > regime.TREND_DISTANCE_ATR  # a later bar changes only later answers


def test_the_threshold_is_the_labs_frozen_h1_value():
    assert regime.TREND_DISTANCE_ATR == 5.0


# -- who is woken ----------------------------------------------------------------------------

def test_flat_ranging_bar_sleeps_and_flat_trending_bar_wakes():
    assert not decide(snap(move=1.0), fills_seen=0).wake
    assert decide(snap(move=6.0), fills_seen=0).wake
    assert not decide(snap(atr=None), fills_seen=0).wake  # unknown regime: no entry without the facts


@pytest.mark.parametrize("kw, extra", [
    (dict(positions=[{"symbol": "SPY", "position": 5}]), {}),
    (dict(orders=[{"orderId": 1}]), {}),
    (dict(mtc=25.0), {}),
    (dict(mtc=0.0), {}),
    (dict(fills=[{"side": "BOT"}]), {}),
    ({}, dict(holding=True)),
])
def test_safety_cases_wake_even_on_a_ranging_bar(kw, extra):
    v = decide(snap(move=0.0, **kw), fills_seen=0, **extra)
    assert v.regime == RANGING and v.wake and v.why.startswith("safety")


def test_first_bar_and_a_new_fill_wake_but_an_old_fill_does_not():
    fill = [{"side": "SELL"}]
    assert decide(snap(), fills_seen=None).wake
    assert decide(snap(fills=fill), fills_seen=0).wake  # a trade just ended: post-trade review
    assert not decide(snap(fills=fill), fills_seen=1).wake  # the same fill, already reviewed


def test_far_from_the_close_is_not_the_exit_window():
    assert not decide(snap(mtc=31.0), fills_seen=0).wake


# -- the heartbeat ---------------------------------------------------------------------------

def test_gate_off_by_default_every_bar_reaches_the_model(rig):
    r = rig()
    r.hb.tick()
    assert len(r.model.calls) == 1 and "regime_gate" not in r.records()[0]


def test_gate_on_a_flat_ranging_bar_is_not_sent_to_the_model_and_is_journaled_as_wait(rig):
    r = rig(regime_gate=True)
    r.hb.tick()  # the first bar always wakes
    assert len(r.model.calls) == 1
    r.feed.shown += 1
    r.now += pd.Timedelta(minutes=5)
    rec = r.hb.tick()[0]
    assert len(r.model.calls) == 1 and rec["model_asked"] is False
    assert rec["decision"]["action"] == "wait" and rec["verdict"]["allowed"] and not r.ib.placed
    assert rec["regime_gate"]["wake"] is False and rec["regime_gate"]["regime"] == RANGING
    assert r.state.last_bar_ts == r.feed.poll().index[-1].isoformat()  # the bar counts as handled
    assert rec["snapshot_sha1"] and (r.journal.root / "snapshots").glob("*.json")


def test_gate_on_a_held_long_still_reaches_the_model_on_every_bar(rig):
    r = rig(answers=[WAIT], regime_gate=True)
    r.ib.holdings["SPY"] = 5
    for _ in range(3):
        r.hb.tick()
        r.feed.shown += 1
        r.now += pd.Timedelta(minutes=5)
    assert len(r.model.calls) == 3
    assert all(x["regime_gate"]["wake"] for x in r.records())


def test_gate_on_the_skipped_bars_arrive_in_the_next_delta(rig):
    r = rig(regime_gate=True)
    r.hb.tick()
    for _ in range(3):  # three flat ranging bars while asleep
        r.feed.shown += 1
        r.now += pd.Timedelta(minutes=5)
        r.hb.tick()
    assert len(r.model.calls) == 1
    r.ib.holdings["SPY"] = 5  # a long appears (e.g. a fill): the next bar wakes the model
    r.feed.shown += 1
    r.now += pd.Timedelta(minutes=5)
    r.hb.tick()
    sent = r.model.calls[-1]
    assert len(r.model.calls) == 2 and sent["snapshot_kind"] == "delta" and len(sent["new_bars_5min"]) == 4
