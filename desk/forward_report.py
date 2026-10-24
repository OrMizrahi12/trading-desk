"""The forward-test report: the trader against three baselines, as fixed in experiments/PROTOCOL.md.

`uv run -m desk.forward_report [--unseal] [--write] [--table]`

Forward test = judged only on days that come after the rules were written down, so nothing can be tuned to what happened.
The numbers below are the protocol's; PROTOCOL.md names them and tests/test_forward_report.py checks the two agree.

SEALED by default: until the stop rule is reached (MIN_TRADES scored trades, or the last day of the period) the report
prints only data quality (days, bars covered, number of trades) and never a profit, loss or ratio. `--unseal` prints
everything anyway, headed EXPLORATORY: those numbers must not be quoted as the result. `--table` prints the Sharpe table
that PROTOCOL.md contains. `--write` saves the report as experiments/forward-report.md (only when unsealed or final).

Terms:
- bar = open, high, low, close and volume of one 5-minute slice; stop = a resting order that sells at the loss limit;
  target = a resting order that sells at the profit goal.
- R = the money lost if the stop is hit, so a trade's result in R = (exit - entry) / (entry - stop), costs included.
  Every arm is counted in R, because the lab caps the paper position at a few shares and dollar returns of the account
  would be meaningless.
- Sharpe ratio = average daily result divided by how much it bounces around, times sqrt(252) (trading days in a year).
- bootstrap = re-drawing the same days or trades with replacement thousands of times to see how far a number could move.
Paper trading only; nothing here touches a broker.
"""

import argparse
import json
import math
import subprocess
import sys
import zlib
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

from . import lab  # noqa: F401
from backtests.engine import CostModel, run_backtest
from strategies.opening_range_breakout import OpeningRangeBreakout

from .journal import DESK_DIR, NY, Journal, bar_day

# --- the protocol's numbers (experiments/PROTOCOL.md repeats them; a test keeps the two equal) ---------------------------
VERSION = "Trader v2.2 + settled bars"  # the ledger row under test
INSTRUCTIONS_HASH = "3671b4756f4f"
SYMBOL = "SPY"
START = "2026-10-26"  # first trading day counted (a Monday; the protocol was written on Saturday 2026-10-24)
MAX_DAYS = 120  # trading days in the period, START included
MIN_TRADES = 100  # scored closed trades needed for a verdict
MIN_DAY_COVERAGE = 0.95  # share of a day's entry-window bars that must have been decided and be in the data, else the day is dropped
ENTRY_FROM_MIN = 15  # the desk takes no entry in the first 15 minutes ...
ENTRY_UNTIL_MIN = 60  # ... nor when fewer than 60 minutes are left to the close
FLATTEN_MIN = 10  # the desk is flat 10 minutes before the close
MAX_DROPPED = 0.25  # random draws dropped for overlap before the report flags a degraded frequency match
MAX_UNSCORED = 0.10  # more trades than this share without a known result = inconclusive
DRAWS = 2000  # random-entry replicates
SEED = 20261026
ALPHA, POWER = 0.05, 0.80  # one-sided test; chance of seeing a real edge when it exists
TRADING_DAYS = 252
BAR = pd.Timedelta(minutes=5)
SNAPSHOTS = DESK_DIR / "snapshots"
LEDGER = Path(__file__).parent.parent / "experiments" / "ledger.csv"
PROTOCOL = Path(__file__).parent.parent / "experiments" / "PROTOCOL.md"
REPORT = Path(__file__).parent.parent / "experiments" / "forward-report.md"
PLAIN_MODE_KEYS = ("regime_gate", "team", "orb_veto")  # a bar record with any of these ran a different design

NORM = NormalDist()
Z_ALPHA, Z_POWER = NORM.inv_cdf(1 - ALPHA), NORM.inv_cdf(POWER)


# --- how many trades before a Sharpe is distinguishable from luck ----------------------------------------------------------
def trades_needed(annual_sharpe: float, per_year: int = TRADING_DAYS, power_z: float = Z_POWER) -> int:
    """Trades after which a TRUE Sharpe of `annual_sharpe` shows above luck (5% one-sided) with the chosen power.

    Per-trade Sharpe s = annual / sqrt(per_year). The estimate's noise is 1/sqrt(n), so the test statistic s*sqrt(n) must
    clear z_alpha even though the data are noisy: n = ((z_alpha + z_power) / s)^2. `power_z` 0 gives the coin-flip case
    (the estimate lands exactly on the luck line half the time)."""
    s = annual_sharpe / math.sqrt(per_year)
    return math.ceil(((Z_ALPHA + power_z) / s) ** 2)


def sharpe_to_beat_luck(n: int, per_year: int = TRADING_DAYS) -> float:
    """Annualised Sharpe that a strategy with NO edge reaches or passes only 5% of the time over `n` trades."""
    return Z_ALPHA * math.sqrt(per_year) / math.sqrt(n)


def sharpe_tables() -> str:
    """The two tables in PROTOCOL.md, as Markdown."""
    sharpes = (0.5, 1.0, 1.5, 2.0, 3.0, 4.0)
    a = ["| True Sharpe (yearly) | Same per trade | Trades for a 50% chance to see it | Trades for an 80% chance | Months at 1 trade a day (80%) |",
         "| --- | --- | --- | --- | --- |"]
    for s in sharpes:
        n50, n80 = trades_needed(s, power_z=0.0), trades_needed(s)
        a.append(f"| {s:.1f} | {s / math.sqrt(TRADING_DAYS):.3f} | {n50:,} | {n80:,} | {n80 / 21:.1f} |")
    b = ["| Trades so far | Yearly Sharpe needed to beat luck (5% one-sided) |", "| --- | --- |"]
    for n in (10, 20, 30, 50, 100, 200, 500, 1000):
        b.append(f"| {n:,} | {sharpe_to_beat_luck(n):.2f} |")
    return "\n".join(a) + "\n\n" + "\n".join(b)


# --- the calendar and the data ---------------------------------------------------------------------------------------------
def calendar():
    import exchange_calendars as xcals
    return xcals.get_calendar("XNYS")


def period_days(start: str = START, n: int = MAX_DAYS) -> list[str]:
    """The `n` trading days from `start` on (NYSE calendar: holidays out)."""
    cal = calendar()
    sessions = cal.sessions_in_range(pd.Timestamp(start), pd.Timestamp(start) + pd.Timedelta(days=n * 2 + 30))
    return [d.date().isoformat() for d in sessions[:n]]


def load_stitched_bars(snapshots: Path = SNAPSHOTS) -> pd.DataFrame:
    """One 5-minute series: each bar from the NEWEST snapshot that holds it (UTC index, open/high/low/close/volume)."""
    best: dict[pd.Timestamp, dict] = {}
    for p in sorted(snapshots.glob("*.json")):
        if p.name.endswith(".delta.json"):
            continue
        for r in json.loads(p.read_text()).get("bars_5min") or []:
            ts = pd.Timestamp(r["start"])
            best[ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")] = r
    if not best:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    idx = sorted(best)
    return pd.DataFrame([{k: best[t][k] for k in ("open", "high", "low", "close", "volume")} for t in idx],
                        index=pd.DatetimeIndex(idx, name="start"))


def plain_bar_records(journal: Journal) -> dict[str, dict]:
    """Bar records of the version under test run the plain way, by bar start (ISO, UTC)."""
    return {r["bar"]: r for r in journal.bar_records()
            if r.get("instructions") == INSTRUCTIONS_HASH and not any(k in r for k in PLAIN_MODE_KEYS)}


def day_window(day: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    cal = calendar()
    return cal.session_open(pd.Timestamp(day)), cal.session_close(pd.Timestamp(day))


def entry_bar_starts(day: str) -> list[pd.Timestamp]:
    """Starts of the bars whose close can carry an entry decision under the desk's gates."""
    open_, close = day_window(day)
    out, t = [], open_
    while t + BAR <= close:
        if t + BAR >= open_ + pd.Timedelta(minutes=ENTRY_FROM_MIN) and t + BAR <= close - pd.Timedelta(minutes=ENTRY_UNTIL_MIN):
            out.append(t)
        t += BAR
    return out


def day_quality(days: list[str], bars: pd.DataFrame, records: dict[str, dict]) -> dict[str, dict]:
    """Per day: how much of the entry window the data and the trader's journal cover, and whether the day counts."""
    rec_starts = {pd.Timestamp(k).tz_convert("UTC") for k in records}
    out = {}
    for day in days:
        starts = entry_bar_starts(day)
        if not starts or not len(bars):
            out[day] = {"expected": len(starts), "in_data": 0, "decided": 0, "valid": False}
            continue
        in_data = sum(1 for t in starts if t in bars.index)
        decided = sum(1 for t in starts if t in rec_starts)
        out[day] = {"expected": len(starts), "in_data": in_data, "decided": decided,
                    "valid": in_data >= MIN_DAY_COVERAGE * len(starts) and decided >= MIN_DAY_COVERAGE * len(starts)}
    return out


# --- the trader --------------------------------------------------------------------------------------------------------
def trader_trades(journal: Journal, records: dict[str, dict], valid: set[str]) -> list[dict]:
    """Closed trades of the version under test, plain mode, entered on a valid day. `result_r` None = no known result."""
    out = []
    for t in journal.closed_trades():
        if t.get("entry_bar") in records and bar_day(t["entry_bar"]) in valid:
            out.append(t)
    return out


def stop_geometry(t: dict) -> tuple[float, float | None] | None:
    """(stop distance as a share of the entry price, target distance in multiples of the stop distance or None)."""
    e, s, tg = t.get("entry_price"), t.get("stop"), t.get("target")
    if not e or s is None or s >= e:
        return None
    return (e - s) / e, (None if tg is None else (tg - e) / (e - s))


def daily_series(days: list[str], results: list[tuple[str, float]]) -> pd.Series:
    """Sum of R per valid day, 0 on days without a trade (those days count: a patient strategy is flat on them)."""
    s = pd.Series(0.0, index=days)
    for d, r in results:
        s[d] += r
    return s


def sharpe(daily: pd.Series | np.ndarray) -> float:
    x = np.asarray(daily, float)
    if len(x) < 2 or x.std(ddof=1) < 1e-9 * max(1.0, abs(x.mean())):  # constant series: float noise, not a spread
        return float("nan")
    return float(x.mean() / x.std(ddof=1) * math.sqrt(TRADING_DAYS))


def boot_ci(values: np.ndarray, stat, rng: np.random.Generator, draws: int = 5000) -> tuple[float, float] | None:
    if len(values) < 2:
        return None
    out = np.array([stat(rng.choice(values, size=len(values), replace=True)) for _ in range(draws)])
    out = out[~np.isnan(out)]
    return (float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5))) if len(out) else None


# --- baseline 1: random entries matched on frequency and risk ----------------------------------------------------------------
COSTS = CostModel()


def simulate_long(day_bars: pd.DataFrame, decide_idx: int, stop_pct: float, target_mult: float | None,
                  close_ts: pd.Timestamp) -> tuple[float, int] | None:
    """One long: decided on bar `decide_idx`, filled at the next bar's open, stop first, flat FLATTEN_MIN before the close.

    Same arithmetic as the lab's backtest engine: a buy pays half the spread plus slippage, a sale likewise (a target pays no
    slippage), commission both ways; R = net per share / (fill - stop + round-trip costs), so a stop-out is about -1R.
    Returns (result in R, index of the exit bar) or None when no bar is left to enter on."""
    i = decide_idx + 1
    flat_at = close_ts - pd.Timedelta(minutes=FLATTEN_MIN)
    if i >= len(day_bars) or day_bars.index[i] >= flat_at:
        return None
    o, h, l = (day_bars[c].to_numpy(float) for c in ("open", "high", "low"))
    raw = o[i]
    fill = COSTS.buy(raw)
    stop = raw * (1 - stop_pct)
    target = None if target_mult is None else raw + target_mult * (raw - stop)
    risk_ps = fill - stop + 2 * COSTS.commission_per_share + COSTS.spread / 2 + COSTS.slippage_per_share
    if risk_ps <= 0:
        return None
    exit_px, exit_i = None, len(day_bars) - 1
    for j in range(i, len(day_bars)):
        if day_bars.index[j] >= flat_at:
            exit_px, exit_i = COSTS.sell(o[j]), j
            break
        if l[j] <= stop:
            exit_px, exit_i = COSTS.sell(min(stop, o[j])), j
            break
        if target is not None and h[j] >= target:
            exit_px, exit_i = COSTS.sell_limit(max(target, o[j])), j
            break
    if exit_px is None:
        exit_px = COSTS.sell(float(day_bars["close"].iloc[-1]))
    net = exit_px - fill - 2 * COSTS.commission_per_share
    return net / risk_ps, exit_i


def random_baseline(valid_days: list[str], bars: pd.DataFrame, trades: list[dict], draws: int | None = None, seed: int = SEED) -> dict:
    """`draws` replicates of: on every valid day, as many entries as the trader made that day, at random eligible bars, each
    with the stop distance and target multiple of the matching trader trade (same order within the day). No overlapping
    trades: a draw that falls inside the previous random trade is dropped and counted."""
    draws = DRAWS if draws is None else draws
    rng = np.random.default_rng(seed)
    per_day: dict[str, list[tuple[float, float | None]]] = {}
    for t in sorted(trades, key=lambda x: x["entry_bar"]):
        g = stop_geometry(t)
        if g:
            per_day.setdefault(bar_day(t["entry_bar"]), []).append(g)
    day_data = {}
    for d in valid_days:
        if d in per_day:
            ts = bars.index.tz_convert(NY).date == pd.Timestamp(d).date()
            db = bars[ts]
            elig = [db.index.get_loc(t) for t in entry_bar_starts(d) if t in db.index]
            day_data[d] = (db, elig, day_window(d)[1])
    means, sharpes, totals, dropped, made = [], [], [], 0, 0
    for _ in range(draws):
        res = []
        for d, geoms in per_day.items():
            if d not in day_data:
                continue
            db, elig, close_ts = day_data[d]
            if len(elig) < len(geoms):
                continue
            picks = sorted(rng.choice(elig, size=len(geoms), replace=False))
            free_from = -1
            for pick, (sp, tm) in zip(picks, geoms):
                if pick <= free_from:
                    dropped += 1
                    continue
                out = simulate_long(db, int(pick), sp, tm, close_ts)
                if out is None:
                    dropped += 1
                    continue
                res.append((d, out[0]))
                free_from = out[1]
                made += 1
        rs = [r for _, r in res]
        means.append(float(np.mean(rs)) if rs else float("nan"))
        totals.append(float(np.sum(rs)))
        sharpes.append(sharpe(daily_series(valid_days, res)))
    return {"mean_r": np.array(means), "total_r": np.array(totals), "sharpe": np.array(sharpes),
            "dropped": dropped, "made": made, "draws": draws}


# --- baselines 2 and 3 -------------------------------------------------------------------------------------------------------
def buy_and_hold(valid_days: list[str], bars: pd.DataFrame) -> dict:
    """Buy SPY at each valid day's open and sell at its close (flat overnight like the desk), and the plain version:
    one buy at the first open and one sale at the last close, overnights included. Returns in percent, no costs."""
    daily = {}
    for d in valid_days:
        db = bars[bars.index.tz_convert(NY).date == pd.Timestamp(d).date()]
        if len(db):
            daily[d] = (db["close"].iloc[-1] / db["open"].iloc[0] - 1) * 100
    series = pd.Series(daily)
    first = bars[bars.index.tz_convert(NY).date == pd.Timestamp(valid_days[0]).date()] if valid_days else bars.iloc[:0]
    last = bars[bars.index.tz_convert(NY).date == pd.Timestamp(valid_days[-1]).date()] if valid_days else bars.iloc[:0]
    whole = (last["close"].iloc[-1] / first["open"].iloc[0] - 1) * 100 if len(first) and len(last) else float("nan")
    return {"daily_pct": series, "sharpe": sharpe(series), "total_pct_intraday": float(series.sum()), "total_pct_whole": float(whole)}


def orb_baseline(valid_days: list[str], bars: pd.DataFrame) -> dict:
    """The lab's opening_range_breakout, default settings, run by the lab's own backtest engine on the valid days only.
    R = net profit / what a stop-out would cost (costs included), as in the lab's engine."""
    mask = pd.Index(bars.index.tz_convert(NY).date.astype(str)).isin(valid_days)
    sub = bars[mask]
    if sub.empty:
        return {"results": [], "sharpe": float("nan")}
    res = run_backtest(sub, OpeningRangeBreakout(), costs=COSTS)
    tr = res.trades
    results = []
    if not tr.empty:
        for _, row in tr.iterrows():
            if row["stop_risk"] > 0:
                results.append((row["entry_time"].tz_convert(NY).date().isoformat(), float(row["net_pnl"] / row["stop_risk"])))
    return {"results": results, "sharpe": sharpe(daily_series(valid_days, results))}


# --- stop rule, protocol integrity, report --------------------------------------------------------------------------------
def protocol_commits(path: Path = PROTOCOL) -> list[str]:
    try:
        out = subprocess.run(["git", "log", "--format=%h %cI", "--", str(path)], cwd=path.parent, capture_output=True, text=True,
                             timeout=20).stdout
    except Exception:
        return []
    return [line for line in out.splitlines() if line]


def ledger_row_ok(ledger: Path = LEDGER) -> bool:
    import csv
    with open(ledger, newline="") as f:
        return any(r["version"] == VERSION and r["instructions_file_hash"] == INSTRUCTIONS_HASH for r in csv.DictReader(f))


def stop_reached(scored: int, days: list[str], today: str) -> str | None:
    if scored >= MIN_TRADES:
        return f"{scored} scored trades (the minimum is {MIN_TRADES})"
    if days and today > days[-1]:
        return f"the period ended on {days[-1]}"
    return None


def fmt(x, nd=2) -> str:
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.{nd}f}"


def build_report(*, journal: Journal, bars: pd.DataFrame, today: str, unseal: bool = False) -> str:
    days = period_days()
    days_so_far = [d for d in days if d <= today]
    records = plain_bar_records(journal)
    quality = day_quality(days_so_far, bars, records)
    valid = [d for d in days_so_far if quality[d]["valid"]]
    trades = trader_trades(journal, records, set(valid))
    scored = [t for t in trades if t.get("result_r") is not None]
    reason = stop_reached(len(scored), days, today)
    sealed = reason is None and not unseal
    head = [f"# Forward test: {VERSION} (instructions hash {INSTRUCTIONS_HASH})", "",
            f"Protocol: experiments/PROTOCOL.md. Period: {days[0]} to {days[-1]} ({MAX_DAYS} trading days). Report date {today}.",
            f"Ledger row present with this hash: {'yes' if ledger_row_ok() else 'NO - STOP, the ledger does not match'}.",
            f"Protocol commits (newest first): {', '.join(protocol_commits()) or 'none found (uncommitted?)'}.", ""]
    if len(protocol_commits()) > 1:
        head += ["**WARNING: PROTOCOL.md changed after it was first committed. Read the git log before trusting this report.**", ""]
    body = ["## Data quality (always shown)", "",
            f"- Trading days so far: {len(days_so_far)}; days that count: {len(valid)} "
            f"(a day needs {MIN_DAY_COVERAGE:.0%} of its entry-window bars both in the data and decided on by the trader).",
            f"- Closed trades of this version in plain mode on counted days: {len(trades)}; with a known result: {len(scored)}; "
            f"without ({len(trades) - len(scored)}) they are reported, never guessed.",
            f"- Dropped days: {', '.join(d for d in days_so_far if not quality[d]['valid']) or 'none'}.", ""]
    if sealed:
        need = MIN_TRADES - len(scored)
        body += ["## Results: SEALED", "",
                 f"No profit, loss, ratio or comparison is shown until the stop rule is reached ({MIN_TRADES} scored trades, or the "
                 f"end of the period on {days[-1]}). {need} more scored trades needed. `--unseal` shows exploratory numbers, "
                 "which are not the result.", ""]
        return "\n".join(head + body)

    status = (f"FINAL: stop rule reached ({reason})." if reason else
              "EXPLORATORY (unsealed before the stop rule): do not quote these numbers as the result.")
    rs = np.array([t["result_r"] for t in scored], float)
    unscored_share = (len(trades) - len(scored)) / len(trades) if trades else 0.0
    rng = np.random.default_rng(zlib.crc32(b"forward-report"))
    daily = daily_series(valid, [(bar_day(t["entry_bar"]), t["result_r"]) for t in scored])
    out = ["## Results: " + status, ""]
    if len(scored) < MIN_TRADES:
        out += [f"**Under the minimum: {len(scored)} scored trades of {MIN_TRADES}. Inconclusive by the protocol's own rule; "
                f"at this size only a yearly Sharpe above {sharpe_to_beat_luck(max(len(scored), 1)):.2f} could be told from luck.**", ""]
    if unscored_share > MAX_UNSCORED:
        out += [f"**{unscored_share:.0%} of the trades have no known result (limit {MAX_UNSCORED:.0%}): inconclusive.**", ""]
    ci = boot_ci(rs, np.mean, rng)
    rb = random_baseline(valid, bars, scored)
    orb = orb_baseline(valid, bars)
    bh = buy_and_hold(valid, bars)
    my_mean = float(rs.mean()) if len(rs) else float("nan")
    valid_means = rb["mean_r"][~np.isnan(rb["mean_r"])]
    p = (float((valid_means >= my_mean).sum()) + 1) / (len(valid_means) + 1) if len(valid_means) and not math.isnan(my_mean) else float("nan")
    out += ["### Primary test (the only one that carries a verdict)", "",
            f"Trader average result per trade: {fmt(my_mean)} R (95% bootstrap range {fmt(ci[0]) + ' to ' + fmt(ci[1]) if ci else 'n/a'}). "
            f"Random entries matched on frequency and risk, {len(valid_means):,} replicates: median {fmt(float(np.median(valid_means)) if len(valid_means) else None)} R, "
            f"95th percentile {fmt(float(np.percentile(valid_means, 95)) if len(valid_means) else None)} R.",
            f"One-sided p-value (share of random replicates at least as good, +1 for the observed one): {fmt(p, 3)}. "
            f"Verdict rule: skill is shown only if p < {ALPHA}, at least {MIN_TRADES} scored trades and at most {MAX_UNSCORED:.0%} unscored.", ""]
    verdict = ("INCONCLUSIVE" if len(scored) < MIN_TRADES or unscored_share > MAX_UNSCORED or math.isnan(p)
               else "NOT DISTINGUISHABLE FROM LUCK" if p >= ALPHA else "BETTER THAN LUCK at the 5% level")
    out += [f"**Verdict: {verdict}.**" + (" (exploratory)" if reason is None else ""), "",
            "### Comparison table (daily Sharpe: each valid day counts, flat days are 0)", "",
            "| Arm | Trades | Average R per trade | Total R | Daily Sharpe (yearly) | Sharpe 95% range (days resampled) |",
            "| --- | --- | --- | --- | --- | --- |"]

    def row(name, results, series=None, mean_r=True):
        s = series if series is not None else daily_series(valid, results)
        rr = np.array([r for _, r in results], float)
        rngl = np.random.default_rng(zlib.crc32(name.encode()))
        rng_ci = boot_ci(np.asarray(s, float), sharpe_np, rngl, 2000)
        return (f"| {name} | {len(results) if mean_r else '-'} | {fmt(float(rr.mean())) if len(rr) else 'n/a'} | "
                f"{fmt(float(rr.sum())) if len(rr) else 'n/a'} | {fmt(sharpe(s))} | "
                f"{(fmt(rng_ci[0]) + ' to ' + fmt(rng_ci[1])) if rng_ci else 'n/a'} |")

    out.append(row("Trader", [(bar_day(t['entry_bar']), t['result_r']) for t in scored]))
    out.append(f"| Random entries, matched (median of {len(valid_means):,}) | {rb['made'] / rb['draws']:.1f} | "
               f"{fmt(float(np.median(valid_means)) if len(valid_means) else None)} | {fmt(float(np.median(rb['total_r'])))} | "
               f"{fmt(float(np.nanmedian(rb['sharpe'])) if len(rb['sharpe']) else None)} | "
               f"{fmt(float(np.nanpercentile(rb['sharpe'], 2.5)) if len(rb['sharpe']) else None)} to "
               f"{fmt(float(np.nanpercentile(rb['sharpe'], 97.5)) if len(rb['sharpe']) else None)} (spread of replicates) |")
    out.append(row("ORB (lab default) on the same days", orb["results"]))
    bs = bh["daily_pct"]
    brng = boot_ci(np.asarray(bs, float), sharpe_np, np.random.default_rng(zlib.crc32(b"bh")), 2000)
    out.append(f"| Buy-and-hold SPY, open to close each day | - | - | - | {fmt(bh['sharpe'])} | "
               f"{(fmt(brng[0]) + ' to ' + fmt(brng[1])) if brng else 'n/a'} |")
    out += ["", f"Buy-and-hold in percent: open-to-close days added up {fmt(bh['total_pct_intraday'])}%; one buy at the first open and one "
            f"sale at the last close {fmt(bh['total_pct_whole'])}%. Buy-and-hold has no stop, so it has no R.",
            f"Random arm: {rb['made'] / rb['draws']:.1f} trades per replicate on average; {rb['dropped'] / rb['draws']:.1f} per replicate dropped (overlap or no bar left)."
            + (f" **More than {MAX_DROPPED:.0%} of the draws were dropped: the frequency match is degraded, read the primary test with care.**"
               if rb["dropped"] > MAX_DROPPED * (rb["dropped"] + rb["made"]) else ""),
            "", "The ORB and buy-and-hold rows are context for a reader, not tests: no claim 'beats ORB' or 'beats the market' is allowed "
            "unless the Sharpe ranges above do not overlap, and even then it is a hypothesis for the next experiment.", ""]
    return "\n".join(head + body + out)


def sharpe_np(x: np.ndarray) -> float:
    return sharpe(x)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--unseal", action="store_true", help="show results before the stop rule (exploratory, not the result)")
    ap.add_argument("--write", action="store_true", help=f"save the report as {REPORT.name} (only when unsealed or final)")
    ap.add_argument("--table", action="store_true", help="print the Sharpe tables of PROTOCOL.md and exit")
    ap.add_argument("--today", default=None, help="report date YYYY-MM-DD (default: today in New York)")
    ap.add_argument("--snapshots", type=Path, default=SNAPSHOTS)
    ap.add_argument("--journal-dir", type=Path, default=DESK_DIR)
    args = ap.parse_args()
    if args.table:
        print(sharpe_tables())
        return
    today = args.today or pd.Timestamp.now(tz=NY).date().isoformat()
    text = build_report(journal=Journal(args.journal_dir), bars=load_stitched_bars(args.snapshots), today=today, unseal=args.unseal)
    print(text)
    if args.write:
        if "Results: SEALED" in text:
            sys.exit("sealed: nothing written")
        REPORT.write_text(text + "\n")
        print(f"\nwritten to {REPORT}")


if __name__ == "__main__":
    main()
