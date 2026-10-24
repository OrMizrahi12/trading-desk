"""The forward-test report: sealed until the stop rule, the Sharpe table is right, the baselines run, PROTOCOL.md agrees."""

import json
import re

import numpy as np
import pandas as pd
import pytest

from desk import forward_report as fr
from desk.journal import Journal


def test_protocol_names_the_numbers_the_script_uses():
    text = fr.PROTOCOL.read_text()
    for needle in (fr.VERSION, fr.INSTRUCTIONS_HASH, fr.START, fr.period_days()[-1], str(fr.MAX_DAYS), str(fr.MIN_TRADES),
                   str(fr.SEED), f"{fr.DRAWS:,}"):
        assert needle in text, needle
    assert fr.sharpe_tables() in text  # the table in the protocol is the one the code produces
    assert fr.ledger_row_ok()


def test_sharpe_table_matches_simulation():
    """True yearly Sharpe 2.0 over trades_needed(2.0) trades should clear the 5% luck line about 80% of the time."""
    n, s = fr.trades_needed(2.0), 2.0 / np.sqrt(fr.TRADING_DAYS)
    rng = np.random.default_rng(1)
    x = rng.normal(s, 1.0, size=(4000, n))
    stat = x.mean(axis=1) / x.std(axis=1, ddof=1) * np.sqrt(n)
    assert abs((stat > fr.Z_ALPHA).mean() - fr.POWER) < 0.03
    luck = rng.normal(0.0, 1.0, size=(4000, 100))
    stat0 = luck.mean(axis=1) / luck.std(axis=1, ddof=1) * np.sqrt(100)
    assert abs((stat0 > fr.Z_ALPHA).mean() - fr.ALPHA) < 0.015
    assert fr.trades_needed(1.0) > fr.trades_needed(2.0) > fr.trades_needed(4.0)
    assert fr.sharpe_to_beat_luck(100) == pytest.approx(2.61, abs=0.01)


def test_simulated_long_stop_out_is_about_minus_one_r_and_target_about_two():
    close = pd.Timestamp("2026-10-26 20:00", tz="UTC")
    idx = pd.date_range("2026-10-26 14:30", periods=78, freq="5min", tz="UTC")

    def day(path):
        px = np.array(path, float)
        return pd.DataFrame({"open": px, "high": px + 0.05, "low": px - 0.05, "close": px, "volume": 1000}, index=idx)

    down = np.r_[np.full(10, 100.0), np.linspace(100, 95, 68)]
    r, _ = fr.simulate_long(day(down), 9, 0.005, 2.0, close)
    assert -1.4 < r < -0.9
    up = np.r_[np.full(10, 100.0), np.linspace(100, 108, 68)]
    r, _ = fr.simulate_long(day(up), 9, 0.005, 2.0, close)
    assert 1.7 < r < 2.1
    assert fr.simulate_long(day(up), 77, 0.005, 2.0, close) is None  # no bar left to enter on


def synthetic(tmp_path, monkeypatch, n_days=3, per_day=2, extra=None):
    """Bars and a journal for the first `n_days` period days, a plain-mode trade per entry, and a team-mode decoy."""
    days = fr.period_days()[:n_days]
    rng = np.random.default_rng(5)
    rows, recs, trades = [], [], []
    for d in days:
        open_, close = fr.day_window(d)
        t, px = open_, 600.0
        while t < close:
            px += rng.normal(0, 0.3)
            rows.append({"start": t, "open": px, "high": px + 0.3, "low": px - 0.3, "close": px + 0.05, "volume": 1000})
            t += fr.BAR
        for t in fr.entry_bar_starts(d):
            recs.append({"bar": t.isoformat(), "instructions": fr.INSTRUCTIONS_HASH, "decision": {"action": "wait"}})
        for k in range(per_day):
            eb = fr.entry_bar_starts(d)[10 + 8 * k].isoformat()
            trades.append({"trade_closed": {"ref": f"{d}-{k}", "entry_bar": eb, "entry_price": 600.0, "stop": 598.0, "target": 604.0,
                                            "result_r": [1.5, -1.0][k % 2], "day": d, "shares": 5}, "at": eb})
    bars = pd.DataFrame(rows).set_index("start")
    j = Journal(tmp_path)
    for r in recs + trades + (extra or []):
        j.append(r)
    return days, bars, j


def test_sealed_report_shows_no_result(tmp_path, monkeypatch):
    days, bars, j = synthetic(tmp_path, monkeypatch)
    text = fr.build_report(journal=j, bars=bars, today=days[-1])
    assert "SEALED" in text and "Data quality" in text
    assert "Sharpe" not in text.split("Data quality")[1] and "Verdict" not in text
    assert "6 scored" not in text and "known result: 6" in text  # counts are fine, outcomes are not


def test_unsealed_report_runs_all_arms_and_is_inconclusive_below_the_minimum(tmp_path, monkeypatch):
    monkeypatch.setattr(fr, "DRAWS", 50)
    days, bars, j = synthetic(tmp_path, monkeypatch)
    text = fr.build_report(journal=j, bars=bars, today=days[-1], unseal=True)
    assert "EXPLORATORY" in text and "INCONCLUSIVE" in text
    for arm in ("| Trader |", "Random entries, matched", "ORB (lab default)", "Buy-and-hold SPY"):
        assert arm in text
    assert "Under the minimum: 6 scored trades" in text


def test_final_report_when_the_minimum_is_reached(tmp_path, monkeypatch):
    monkeypatch.setattr(fr, "DRAWS", 50)
    monkeypatch.setattr(fr, "MIN_TRADES", 6)
    days, bars, j = synthetic(tmp_path, monkeypatch)
    text = fr.build_report(journal=j, bars=bars, today=days[-1])
    assert "FINAL" in text and "Verdict: " in text and "SEALED" not in text


def test_other_designs_and_other_hashes_are_not_counted(tmp_path):
    day = fr.period_days()[0]
    starts = [t.isoformat() for t in fr.entry_bar_starts(day)]
    j = Journal(tmp_path)
    plain, team, other_hash, gate = starts[0], starts[1], starts[2], starts[3]
    j.append({"bar": plain, "instructions": fr.INSTRUCTIONS_HASH})
    j.append({"bar": team, "instructions": fr.INSTRUCTIONS_HASH, "team": {}})
    j.append({"bar": other_hash, "instructions": "000000000000"})
    j.append({"bar": gate, "instructions": fr.INSTRUCTIONS_HASH, "regime_gate": {"wake": False}})
    for ref, bar in (("plain", plain), ("team", team), ("other", other_hash), ("gate", gate)):
        j.append({"trade_closed": {"ref": ref, "entry_bar": bar, "entry_price": 600.0, "stop": 598.0, "target": None,
                                   "result_r": 1.0}, "at": bar})
    got = fr.trader_trades(j, fr.plain_bar_records(j), {day})
    assert [t["ref"] for t in got] == ["plain"]


def test_days_without_enough_decided_bars_are_dropped(tmp_path, monkeypatch):
    days, bars, j = synthetic(tmp_path, monkeypatch)
    keep = set(fr.entry_bar_starts(days[0])[:5])
    lines = [json.loads(x) for x in j.path.read_text().splitlines()]
    j.path.write_text("")
    for r in lines:
        if r.get("bar") and bar_in_day(r["bar"], days[0]) and pd.Timestamp(r["bar"]) not in keep:
            continue
        j.append(r)
    q = fr.day_quality(days, bars, fr.plain_bar_records(j))
    assert q[days[0]]["valid"] is False and q[days[1]]["valid"] is True


def bar_in_day(bar, day):
    return pd.Timestamp(bar).tz_convert("America/New_York").date().isoformat() == day
