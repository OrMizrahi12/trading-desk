"""The regime gate: a deterministic filter that decides, from the snapshot alone, whether Claude is woken on this bar.

Regime = "trending" or "ranging", known at the close of the bar (closed bars only, no look-ahead):

    distance = |last close - today's first open| / ATR(14)       ATR = average true range, the typical size of a 5-minute bar
    trending when distance > TREND_DISTANCE_ATR, ranging otherwise, unknown while the ATR or today's open is missing

The threshold is NOT tuned here and NOT fitted on the desk's snapshots. It is the one live-knowable number the lab fixed
in advance, before seeing any profit figure: hypothesis H1 in ~/trading-lab/research/journal.md, card
research/cards/h1_reversion_trend_day_filter.md (`max_open_distance_atr = 5.0`, helper `open_distance_atr` in
strategies/session_clock.py). Note the lab files this under module 8 (regimes, day types), not module 7 (optimization);
module 7 has no regime threshold. The lab's verdict on H1 was FAIL as a way to improve a reversion strategy, and its card
says not to tune the number. This gate uses it only to decide when to spend a model call, which the lab never tested.

The gate is a switch (`--regime-gate`), off by default. It only decides whether to ASK: once Claude is awake it decides
everything, and the risk gates and seatbelts run exactly as before. Safety wakes always win over the regime: a held long,
an open order, a fill since the last bar and the exit window before the close always wake Claude.

Paper trading only: this file reads a snapshot and returns a yes or a no.
"""

from dataclasses import dataclass

import pandas as pd

TREND_DISTANCE_ATR = 5.0  # lab H1, fixed in advance, do not tune (see above)
EXIT_WINDOW_MINUTES = 30  # always awake this close to the close: the desk flattens 10 minutes before it
NY = "America/New_York"

TRENDING, RANGING, UNKNOWN = "trending", "ranging", "unknown"


@dataclass(frozen=True)
class Verdict:
    wake: bool
    why: str  # one short phrase, journaled
    regime: str
    distance_atr: float | None


def distance_atr(snap: dict) -> float | None:
    """|last close - today's first open| / ATR(14) from the snapshot's 5-minute bars, or None when not known yet."""
    rows = snap.get("bars_5min") or []
    if not rows:
        return None
    last = rows[-1]
    day = pd.Timestamp(last["start"]).tz_convert(NY).date()
    today = [r for r in rows if pd.Timestamp(r["start"]).tz_convert(NY).date() == day]
    atr, close, first_open = last.get("atr14"), last.get("close"), today[0].get("open")
    if not atr or atr <= 0 or close is None or first_open is None:
        return None
    return abs(float(close) - float(first_open)) / float(atr)


def classify(snap: dict) -> tuple[str, float | None]:
    dist = distance_atr(snap)
    if dist is None:
        return UNKNOWN, None
    return (TRENDING if dist > TREND_DISTANCE_ATR else RANGING), dist


def decide(snap: dict, *, fills_seen: int | None, holding: bool = False) -> Verdict:
    """Wake Claude on this bar? `fills_seen` = how many fills today the last handled bar showed (None = unknown, wake).

    `holding` is the desk's own record of an open trade; the snapshot's positions and orders are checked too."""
    regime, dist = classify(snap)

    def v(wake: bool, why: str) -> Verdict:
        return Verdict(wake, why, regime, None if dist is None else round(dist, 2))

    if holding or snap.get("positions"):
        return v(True, "safety: holding a long")
    if snap.get("open_orders"):
        return v(True, "safety: open orders")
    fills = len(snap.get("todays_fills") or [])
    if fills_seen is None or fills != fills_seen:
        return v(True, "safety: first bar, or a fill since the last bar (post-trade review)")
    mtc = (snap.get("session") or {}).get("minutes_to_close")
    if mtc is not None and 0 <= mtc <= EXIT_WINDOW_MINUTES:
        return v(True, "safety: exit window before the close")
    if regime == TRENDING:
        return v(True, "regime: trending")
    return v(False, f"regime: {regime}, flat, nothing held")
