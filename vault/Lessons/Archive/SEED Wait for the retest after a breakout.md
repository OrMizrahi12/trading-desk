---
type: lesson
status: archived
archived: 2026-10-24 (seeded test data, never a real lesson)
seeded: true
seen: 4
last_seen: 2026-10-22
summary: After a breakout bar, wait for the price to come back and hold the broken level (a retest) before buying.
trades: none, seeded
replaces: [[SEED Enter on the breakout bar itself]]
---
# SEED Wait for the retest after a breakout

> [!danger] SEEDED TEST NOTE, NOT A REAL LESSON
> Made up on 2026-10-24 to test the memory budget (`uv run -m desk.memory`). No real trade taught this; the prices and counts below are invented. It must never steer the desk. Real lessons link real notes in `Trades/`.

A breakout is a bar that closes above a level the price could not pass before, such as the high of the first 30 minutes. A retest is the price coming back down to that same level and finding buyers there, so the old ceiling becomes a floor.

The idea: the breakout bar itself is where late buyers pile in and the stop would have to sit far below, under the level. Waiting for the retest gives a stop just under the level, a smaller risk per share, and proof that the level holds.

Invented example: SPY closes a bar 0.1% above the opening-range high, falls back to that high two bars later, and a bar closes green off it. Entry there risks 0.08% to the stop; entry on the breakout bar would have risked 0.25%.

When it does not apply: strong breakouts sometimes never come back, and waiting means missing them. That is accepted: the desk trades patiently and mostly flat, and a missed move costs nothing while a poor entry costs 1R.

Words used: **bar** = open, high, low, close and volume of one 5-minute slice of SPY. **R** = 1% of the account's value, the money lost if the stop is hit. **stop** = a resting order that sells at your loss limit. **target** = a resting order that sells at your profit goal. **ATR** = average true range, the average size of a bar over 14 bars. **breakout** = the price closes above a level it failed to pass before.
