---
type: lesson
status: archived
archived: 2026-10-24 (seeded test data, never a real lesson)
seeded: true
seen: 2
last_seen: 2026-10-21
summary: When the ATR is well above its recent average, use 0.5R or less, because the stop distance is wider.
trades: none, seeded
---
# SEED Halve the size when the ATR is high

> [!danger] SEEDED TEST NOTE, NOT A REAL LESSON
> Made up on 2026-10-24 to test the memory budget (`uv run -m desk.memory`). No real trade taught this; the prices and counts below are invented. It must never steer the desk. Real lessons link real notes in `Trades/`.

ATR (average true range) is the average size of a bar over the last 14 bars. A high ATR means the market is swinging widely, so a stop that is not hit by ordinary noise has to sit farther away from the entry.

Because 1R is a fixed amount of money, a wider stop already means fewer shares. This note adds a second step: when the ATR is more than 1.5 times its own 20-day average, take at most 0.5R, since fast markets also slip and gap through stops.

Invented example: the ATR is 0.35% of price against a usual 0.2%. At 1R the position would lose 1% of the account if the stop is hit at the planned price, but a quick bar can fill the stop lower, so the real loss might reach 1.3R. At 0.5R the same surprise costs 0.65R.

When it does not apply: the constitution says size is typically 0.5R anyway, so this lesson changes little on most days. It matters only on the days the desk is tempted to use the full 1R.

Words used: **bar** = open, high, low, close and volume of one 5-minute slice of SPY. **R** = 1% of the account's value, the money lost if the stop is hit. **stop** = a resting order that sells at your loss limit. **target** = a resting order that sells at your profit goal. **ATR** = average true range, the average size of a bar over 14 bars. **breakout** = the price closes above a level it failed to pass before.
