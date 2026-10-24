---
type: lesson
status: archived
archived: 2026-10-24 (seeded test data, never a real lesson)
seeded: true
seen: 3
last_seen: 2026-10-21
summary: After a gap up (the day opens well above yesterday's close) do not buy the first push; wait for a pullback that holds above the opening price.
trades: none, seeded
---
# SEED Wait for the first pullback after a gap up

> [!danger] SEEDED TEST NOTE, NOT A REAL LESSON
> Made up on 2026-10-24 to test the memory budget (`uv run -m desk.memory`). No real trade taught this; the prices and counts below are invented. It must never steer the desk. Real lessons link real notes in `Trades/`.

A gap up means the first bar of the day opens clearly above the previous day's close. The first push after the open is crowded: many orders waiting overnight fire at once, and the price often gives part of the move back within the next six bars.

The idea: let that burst finish, then look for a pullback (a dip of two or three bars) that stops above the opening price. If it does, the buyers who wanted in at the open are still there, and a stop just under the pullback low is close to the entry.

Invented example: SPY opens 0.6% above yesterday's close, spikes 0.2% in three bars, then dips back to the open and holds for four bars. Buying the push would have put the stop 0.4% away; buying the held pullback puts it 0.1% away, so the same 1R buys four times more shares.

When it does not apply: if the pullback closes under the opening price the gap is being sold and the idea is wrong. Do not average down and do not wait for a second try the same hour; the 15-minute opening rule and the chase rule in the constitution still apply.

Words used: **bar** = open, high, low, close and volume of one 5-minute slice of SPY. **R** = 1% of the account's value, the money lost if the stop is hit. **stop** = a resting order that sells at your loss limit. **target** = a resting order that sells at your profit goal. **ATR** = average true range, the average size of a bar over 14 bars. **breakout** = the price closes above a level it failed to pass before.
