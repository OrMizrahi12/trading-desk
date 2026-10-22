---
name: post-trade-review
description: Use on the first bar after a trade ended (recent_journal or todays_fills shows a stop-out, a target hit, a `close`, or a seatbelt sale), and whenever you must check the cooldown and daily loss limits before another entry. Classifies the outcome, applies the 6-bar cooldown and the 2-loss rule, and states one lesson. Not for judging a trade that is still open (use trade-management).
---

# Post-trade review

Purpose: learn from the trade and stop a loss from turning into a revenge trade. Keep it to a few lines.

## 1. Find what happened
- Look in `recent_journal` for the last `buy` and the exit that followed (`result`, `verdict`), and in `todays_fills`
  for prices. Exit types: stop hit (the resting stop order sold at your loss limit), target hit (resting profit
  order), your own `close` / `sell`, or a desk seatbelt (forced sale, e.g. 10 minutes before the close; logged as
  `seatbelt`).
- Result in R = (exit price - entry price) / (entry price - stop). Positive = win, negative = loss. A full stop-out
  is about -1R (slightly worse with slippage, the gap between expected and actual fill price).

## 2. Apply the counters (these gate every later entry)
- Count today's entries and today's losing trades from `todays_fills` and `recent_journal`.
- After a stop-out: no entry for at least 6 five-minute bars (30 minutes). Count bars from the exit bar.
- 2 losing trades today: no more entries today. Only `wait`.
- 3 entries today: done for the day, win or lose.
- A seatbelt sale at the close is a planning failure, not a trade idea: note that you should have exited earlier.

## 3. Classify (pick one)
- Good trade, good result: followed the checklist and it worked.
- Good trade, bad result: followed the checklist, the market simply went the other way. Fine, a stop did its job.
- Bad trade, good result: luck (e.g. chased a spike, no real stop level). Do not repeat it.
- Bad trade, bad result: a rule was broken. Name the rule.

## 4. One lesson, one sentence
Name the concrete thing to do differently, or "nothing to change". Examples: "Stop was inside normal bar noise
(under 1 atr14, the typical bar size), use a swing low next time"; "Entered 6 minutes after a spike bar: chasing".
Do not invent a pattern from one trade, and do not loosen a rule because of one result.

## 5. Next action
- Cooldown or loss limit active: `wait`, and say how many bars remain in `reason`.
- Otherwise go back to market-read; the next setup must pass entry-checklist from scratch.
- A loss is not a reason to trade, and a win is not a reason to size up. Size stays 0.5R typical, 1R maximum.
