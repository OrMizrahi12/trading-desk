---
type: lesson
status: archived
archived: 2026-10-24 (seeded test data, never a real lesson)
seeded: true
seen: 3
last_seen: 2026-10-22
summary: After the desk's seatbelt (a hard rule in code) has sold a position, take no new entry that day; the situation was already outside the plan.
trades: none, seeded
---
# SEED Be quiet after a seatbelt sale

> [!danger] SEEDED TEST NOTE, NOT A REAL LESSON
> Made up on 2026-10-24 to test the memory budget (`uv run -m desk.memory`). No real trade taught this; the prices and counts below are invented. It must never steer the desk. Real lessons link real notes in `Trades/`.

A seatbelt is a rule in the desk's code that acts even if the trader says otherwise, for example the sale of any long ten minutes before the close, or after the STOP file is switched on. It is a safety device, not a trading decision.

The idea: if a seatbelt had to act, something was off: the data, the connection or the plan. The next entry would be taken in the same unclear situation, so the desk waits until the next day, when the session restarts cleanly.

Invented example: the connection to the broker dropped, a seatbelt sold the long at the last known price, and thirty minutes later the feed returned. The desk's counters and the journal were not yet in agreement, and a fresh buy would have been sized against a wrong picture.

When it does not apply: the end-of-day flatten is expected and not a failure, and then there is no time for a new entry anyway.

Words used: **bar** = open, high, low, close and volume of one 5-minute slice of SPY. **R** = 1% of the account's value, the money lost if the stop is hit. **stop** = a resting order that sells at your loss limit. **target** = a resting order that sells at your profit goal. **ATR** = average true range, the average size of a bar over 14 bars. **breakout** = the price closes above a level it failed to pass before.
