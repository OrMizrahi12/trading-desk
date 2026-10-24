---
type: lesson
status: archived
archived: 2026-10-24 (seeded test data, never a real lesson)
seeded: true
seen: 2
last_seen: 2026-10-22
summary: Between 12:00 and 13:00 New York time volume is thin, so crosses of VWAP in that hour are weaker; wait unless the setup is obvious.
trades: none, seeded
---
# SEED Lunchtime volume dries up so skip the 12 o'clock hour

> [!danger] SEEDED TEST NOTE, NOT A REAL LESSON
> Made up on 2026-10-24 to test the memory budget (`uv run -m desk.memory`). No real trade taught this; the prices and counts below are invented. It must never steer the desk. Real lessons link real notes in `Trades/`.

Volume is the number of shares traded in a bar. Around midday it is usually the lowest of the day, because many traders are away. A thin market moves on small orders, so a price level that looks broken may only have been pushed by a few trades.

The idea: the desk's constitution already says to skip thin data. This note makes it concrete: compare the bar's volume with the same hour on earlier days, and if it is below 70% of that, skip the setup.

Invented example: a VWAP reclaim (the price closing back above the day's volume-weighted average) at 12:25 on 55% of the usual midday volume reversed the next bar. The same pattern at 10:20 on normal volume continued.

When it does not apply: on event days (a central bank announcement at 14:00, say) volume can be heavy at any hour, and then the volume comparison, not the clock, is what counts.

Words used: **bar** = open, high, low, close and volume of one 5-minute slice of SPY. **R** = 1% of the account's value, the money lost if the stop is hit. **stop** = a resting order that sells at your loss limit. **target** = a resting order that sells at your profit goal. **ATR** = average true range, the average size of a bar over 14 bars. **breakout** = the price closes above a level it failed to pass before.
