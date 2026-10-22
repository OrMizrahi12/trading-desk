---
name: trade-management
description: Use on any bar where snapshot `positions` shows a long you hold (or an entry may have just filled). Decides wait / sell part / close - is the idea still valid, has the stop or target been reached, is the close near. Do not use it to look for a new entry (that is market-read then entry-checklist).
---

# Trade management

Manage first, hunt later: with a position open, look at it before anything else. Never open a second position.

## Facts to pull from the snapshot
- Entry: `positions[].avg_cost` and `shares`. Stop and target: the resting orders in `open_orders` (`stop` for the
  stop order, `limit` for the target; a stop is a resting order that sells at your loss limit, a target one that
  sells at your profit goal). Your own earlier `reason` in `recent_journal` states the idea.
- R multiple now = (last close - avg_cost) / (avg_cost - stop). 1R = the amount you risked per share.
  +1R means you are up as much as you risked; -1R means the stop is about to hit.
- Time: `session.minutes_to_close`.

## Decide, in this order
1. Near the close (`minutes_to_close` about 25 or less): `close` now. The desk force-sells 10 minutes before the
   close, which is a failure of planning. Never hold overnight.
2. Idea broken: the 5-minute bar closes below the swing low or ema21 on a wide range, or the 15-minute trend turns
   down, or the stop zone is reached before the stop order fills. Then `close` - do not wait for the stop, and never
   hold on "to get back to even".
3. Target reached or very close: take the profit. `close` unless you are scaling out (below).
4. Up at least +1R and the 5-minute trend still intact: you may `sell` a part (about half) with `size_r` equal to
   half the R still at risk, to lock in gains. The rest rides to the target.
5. Otherwise `wait`. Do not fiddle: no action is the normal action.

## Hard rules
- Never move the stop down or widen it. If the idea is wrong, `close` early instead.
- The desk accepts no adds: do not `buy` while long, and never average down.
- Do not `sell` more than you hold. `sell` only reduces a long; there is no shorting.
- `size_r` is 0 for `close` and `wait`; above 0 for `sell`.
- Trailing the stop up is not available to you here: you can only answer. Use `close` or `sell` to protect gains.

## What to say
One sentence: what the price did against the plan, the R multiple, and why you hold or exit. Example: "Up +1.2R
(1.2 times the money I risked), 5-minute trend still above ema9 (smoothed price), so I hold for the 506 target."
