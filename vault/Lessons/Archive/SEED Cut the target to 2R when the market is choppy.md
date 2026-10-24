---
type: lesson
status: archived
archived: 2026-10-24 (seeded test data, never a real lesson)
seeded: true
seen: 1
last_seen: 2026-10-21
summary: In a choppy market keep the target at exactly 2R (twice the stop distance); a farther target rarely fills, though never go under 2R.
trades: none, seeded
---
# SEED Cut the target to 2R when the market is choppy

> [!danger] SEEDED TEST NOTE, NOT A REAL LESSON
> Made up on 2026-10-24 to test the memory budget (`uv run -m desk.memory`). No real trade taught this; the prices and counts below are invented. It must never steer the desk. Real lessons link real notes in `Trades/`.

The target is the resting order that sells at your profit goal. The constitution says it must be at least twice the stop distance, written 2R. A farther target gives a bigger win if hit but is reached less often.

The idea: when the market is choppy (no clear trend, bars overlapping), the price tends to turn back at the day's range edges before any far target. Setting the target at the minimum 2R takes what the day offers.

Invented example: stop 0.15% below entry, so a 2R target is 0.30% above and a 4R target 0.60% above. The day's range from open to the time of entry was only 0.5%, so the 4R target was out of reach.

When it does not apply: in a clear trend day, a farther target is allowed and sometimes right. The rule never goes below 2R, which the desk refuses to relax whatever the lesson says.

Words used: **bar** = open, high, low, close and volume of one 5-minute slice of SPY. **R** = 1% of the account's value, the money lost if the stop is hit. **stop** = a resting order that sells at your loss limit. **target** = a resting order that sells at your profit goal. **ATR** = average true range, the average size of a bar over 14 bars. **breakout** = the price closes above a level it failed to pass before.
