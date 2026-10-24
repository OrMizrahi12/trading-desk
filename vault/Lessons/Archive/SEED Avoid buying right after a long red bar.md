---
type: lesson
status: archived
archived: 2026-10-24 (seeded test data, never a real lesson)
seeded: true
seen: 1
last_seen: 2026-10-19
summary: After a bar that falls more than 1.5 ATR, wait three bars before any buy; a sharp drop usually keeps shaking out buyers.
trades: none, seeded
---
# SEED Avoid buying right after a long red bar

> [!danger] SEEDED TEST NOTE, NOT A REAL LESSON
> Made up on 2026-10-24 to test the memory budget (`uv run -m desk.memory`). No real trade taught this; the prices and counts below are invented. It must never steer the desk. Real lessons link real notes in `Trades/`.

A red bar is a bar that closes lower than it opened. A long one, more than 1.5 ATR (average bar size over 14 bars) from high to low, means sellers were in a hurry. The bars right after it are often wild: the price bounces, falls again, and bounces.

The idea: a stop placed under the bounce low is hit by the second fall far more often than by chance. Three quiet bars show that the hurry is over and the stop can be placed under a base instead of a spike.

Invented example: a bar drops 0.45% in five minutes, the next bar closes green by 0.2%, and the desk is tempted by a hammer shape. The bar after that makes a new low and would have stopped the trade out within ten minutes.

When it does not apply: if the long red bar happens in the first 15 minutes, the constitution already forbids entries. Late in the day the 60-minute rule rules out entries anyway, so the useful window is mid-morning and early afternoon.

Words used: **bar** = open, high, low, close and volume of one 5-minute slice of SPY. **R** = 1% of the account's value, the money lost if the stop is hit. **stop** = a resting order that sells at your loss limit. **target** = a resting order that sells at your profit goal. **ATR** = average true range, the average size of a bar over 14 bars. **breakout** = the price closes above a level it failed to pass before.
