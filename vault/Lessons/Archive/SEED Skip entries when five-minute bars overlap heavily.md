---
type: lesson
status: archived
archived: 2026-10-24 (seeded test data, never a real lesson)
seeded: true
seen: 2
last_seen: 2026-10-20
summary: When most of the last eight bars overlap each other's ranges, the market is choppy: wait instead of buying a small cross.
trades: none, seeded
---
# SEED Skip entries when five-minute bars overlap heavily

> [!danger] SEEDED TEST NOTE, NOT A REAL LESSON
> Made up on 2026-10-24 to test the memory budget (`uv run -m desk.memory`). No real trade taught this; the prices and counts below are invented. It must never steer the desk. Real lessons link real notes in `Trades/`.

Overlapping bars means each bar's high-to-low range sits mostly inside the previous bar's range. The price is going nowhere, and a cross of an average such as VWAP (the volume-weighted average price of the day) in that state is noise, not a decision by buyers.

A rough test a beginner can do by eye: look at the last eight bars. If at least six of them share more than half of their range with the bar before, call it chop and wait. The market-read skill already calls this verdict chop; this note only adds the eight-bar count.

Invented example: eight bars between 10:40 and 11:15 all trade inside a 0.15% band while a VWAP cross appears on the ninth. A long taken on that cross would hit the 1R stop on the next dip back into the band, because there was no trend to carry it.

When it does not apply: a tight band after a strong trend can be a rest before the next leg, but the desk cannot tell the two apart from bars alone. When in doubt, wait, which the constitution already says.

Words used: **bar** = open, high, low, close and volume of one 5-minute slice of SPY. **R** = 1% of the account's value, the money lost if the stop is hit. **stop** = a resting order that sells at your loss limit. **target** = a resting order that sells at your profit goal. **ATR** = average true range, the average size of a bar over 14 bars. **breakout** = the price closes above a level it failed to pass before.
