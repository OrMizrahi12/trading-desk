---
type: setup
setup: vwap-reclaim
status: known, defined in the lab; no desk trades yet
source: ~/trading-lab/strategies/spy_vwap_reclaim.py, strategies/specs/spy_vwap_reclaim.yaml
---
# VWAP reclaim

Sibling setup: [[Opening range breakout]]. Rules that always apply: [[Constitution risk rules]], [[Lab risk limits]].

**The idea.** **VWAP** is the volume-weighted average price: the average price paid today, weighted by volume. In an
uptrend, a dip under VWAP that is quickly bought back is a sign buyers still defend fair value. *Reclaim* = the price closes
back above VWAP.

## Entry (as the lab defines it)
- The previous 5-minute bar closed at or under VWAP, and this bar closes **above** VWAP (a one-bar cross).
- The **20-bar EMA** (a fast-reacting average of the last 20 closes) is **above** VWAP: the short-term trend is still up.
- Signal bar starts at 09:45 or later and at least 30 minutes before the close in the lab's version.
- At most one entry a day. Needs enough history for the EMA (20 bars) and the **ATR** (average bar size, 14 bars).

## Stop
Just under the recent **swing low** (the lowest low of the last 10 bars today, minus 1 cent), but never more than 1.5 ATR
below the entry. Skip the trade if the stop is at or above the entry, or the entry bar's own low is already under the stop.

## Target
**2R**: entry + 2 x (entry - stop). This already meets the constitution's "at least 2x the stop distance".

## Desk adjustments (from the constitution, not from the lab)
- Flat 10 minutes before the close (the lab's version flattens at 5), and no new entry when `minutes_to_close` is under 60.
- Skip when volume is thin or the data is stale: a VWAP built on a few trades is not a fair-value line.
- Size in R: 0.5R typical, never above 1R. A cross after a spike far above VWAP is chasing: `wait`.

## What we have seen
Nothing yet: no desk trade has used this setup. When one does, add a dated line linking the trade note and say "seen once".
