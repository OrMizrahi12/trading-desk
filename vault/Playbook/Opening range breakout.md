---
type: setup
setup: opening-range-breakout
status: known, defined in the lab; no desk trades yet
source: ~/trading-lab/strategies/opening_range_breakout.py
---
# Opening range breakout

Sibling setup: [[VWAP reclaim]]. Rules that always apply: [[Constitution risk rules]], [[Lab risk limits]].

**The idea.** The *opening range* is the high and low of the first minutes after the 09:30 open (the lab uses the first
5 or 15 minutes). If a 5-minute bar later **closes above the range's high**, the bet is that leaving the range upward starts
a move that lasts. (A **bar** = open, high, low, close and volume of one 5-minute slice.)

## Entry (as the lab defines it)
- The opening range has fully closed and its bars are all present (no data gap).
- A 5-minute bar closes **strictly above** the range high; a close exactly at the high is not a break.
- Only the **first** such bar of the day counts. No re-entry after a stop-out.
- Optional variant: the close must also be above **VWAP** (the day's volume-weighted average price), so the breakout is
  above what the average buyer paid today.
- Long only. No new entries in the last 30 minutes in the lab's version.

## Stop
The **low of the opening range** (a **stop** is a resting order that sells at your loss limit). Skip the trade if that low is not below the entry price.

## Target
The lab's default has none: it holds to the stop or to the flatten just before the close. **The desk may not do that**: the
constitution requires a **target** (a resting order that sells at your profit goal) at least 2x the stop distance away, so
target = entry + 2 x (entry - stop) at minimum. If the opening range is so wide that this target is unrealistic, that is a reason to skip.

## Desk adjustments (from the constitution, not from the lab)
- The constitution blocks entries in roughly the first 15 minutes, so with the 5-minute range the earliest buy is a
  bar that closes after 09:45. Check `minutes_to_close` >= 60 for any new entry.
- Size in R: 0.5R typical, never above 1R. A wide range means a wide stop, which means fewer shares, not a bigger R.
- Do not chase: if the breakout bar is already a spike far above the range, `wait`.

## What we have seen
Nothing yet: no desk trade has used this setup. When one does, add a dated line linking the trade note and say "seen once".
