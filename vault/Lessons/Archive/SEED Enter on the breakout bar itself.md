---
type: lesson
status: superseded
superseded_by: [[SEED Wait for the retest after a breakout]]
resolved: 2026-10-24 (seen 1 time against 4, and newer evidence)
seeded: true
seen: 1
last_seen: 2026-10-20
trades: none, seeded
---
# SEED Enter on the breakout bar itself

> [!danger] SEEDED TEST NOTE, NOT A REAL LESSON
> Made up on 2026-10-24 to test the memory budget (`uv run -m desk.memory`). No real trade taught this; the prices and counts below are invented. It must never steer the desk. Real lessons link real notes in `Trades/`.

A breakout is a bar that closes above a level the price could not pass before. When the volume of that bar is well above the recent average, many buyers agree, and the price often runs without looking back.

The idea: if the desk waits for a retest (the price returning to the broken level) it sits out the best moves, because the strongest breakouts never return. Buying the breakout bar when its volume is at least 1.5 times the average of the last 20 bars captures them.

Invented example: SPY closes a bar 0.12% above the opening-range high on twice the usual volume, and keeps rising for forty minutes without touching the level again. A retest entry would have been left waiting the whole time.

When it does not apply: on thin volume the same bar is often a trap. The stop must still sit under the level, which makes it wider than a retest stop, so the size in R shrinks; the constitution's no-chasing rule still decides whether the bar is already too extended.

Words used: **bar** = open, high, low, close and volume of one 5-minute slice of SPY. **R** = 1% of the account's value, the money lost if the stop is hit. **stop** = a resting order that sells at your loss limit. **target** = a resting order that sells at your profit goal. **ATR** = average true range, the average size of a bar over 14 bars. **breakout** = the price closes above a level it failed to pass before.
