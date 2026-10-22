---
name: entry-checklist
description: Use only when market-read says up / up / confirms / usable and you are about to answer `buy`. Runs the pass-or-wait gates, then picks the stop, the target (at least 2x the stop distance) and the position size in R with the share-count math. Do not use it while you already hold a long (use trade-management) or when you have no setup (just wait).
---

# Entry checklist

A buy needs every gate to pass. One failure means `wait`. When in doubt, `wait`.

## Gates (all must be true)
1. `bars_daily` and `bars_15min` trends both up, latest 5-minute bar confirms (market-read verdict).
2. `session.minutes_to_close` is 60 or more, `stop_file_present` is false, data is fresh (market-read step 0).
3. No position held and no resting buy in `open_orders`. The desk refuses any add to a position, and you never average
   down (buying more of a losing position).
4. Entries today (`todays_fills` and `recent_journal`) are fewer than 3, and fewer than 2 trades lost today.
5. At least 6 bars (30 minutes) since the last stop-out. No revenge trade: a loss is not a reason to trade.
6. Not chasing: last bar range at most about 2x atr14, and price not far above vwap and the 5-minute ema9.

## Stop (name what makes you wrong)
- Put it just below the most recent 5-minute swing low (a bar whose low is below its neighbours' lows), or about
  1 to 1.5 x atr14 below the last close - whichever is the level where the idea is plainly wrong.
- It must be above 0 and below the last close. No such level: no trade.
- Never widen or lower it later.

## Target (at least 2x the stop distance)
- risk_per_share = close - stop (the dollars lost per share if the stop is hit)
- target >= close + 2 x risk_per_share (reward-to-risk of 2: win twice what you risk)
- It must be realistic: at or below a visible resistance (recent high, `bb_upper`) or within about 2-3 atr14 of price.
  If a clear obstacle sits before the 2x level, `wait`.
- `null` only with a reason in `reason`.

## Size in R
- Terms: net liquidation = `account.NetLiquidation` (cash plus positions). 1R = 1% of it = the dollars lost if the
  stop is hit. `size_r` is how many R you risk on this trade.
- Choose `size_r`: 0.5 typical; 0.25-0.35 for an average setup or noisy bars; 1.0 absolute maximum.
- Dollars at risk = size_r x 0.01 x net_liquidation
- The desk assumes a worst fill 0.5% above the last close: fill = close x 1.005
- Shares = floor(dollars_at_risk / (fill - stop)), also capped at floor(net_liquidation / fill) (no leverage) and by
  `risk_limits` (the lab's caps; the desk shrinks or refuses over-limit buys).
- Example: net liquidation 100,000, close 500.00, stop 497.00, size_r 0.5. Dollars at risk = 0.5 x 0.01 x 100,000 =
  500. fill = 502.50. Risk per share = 502.50 - 497.00 = 5.50. Shares = floor(500 / 5.50) = 90. Target >= 500 + 2 x
  3.00 = 506.00. You do not send shares: you send `size_r`, and the desk computes the shares this way.
- If the result is 0 shares, the stop is too far for the account: `wait`.

## Output
`buy`, `size_r`, `stop`, `target`, and a `reason` under 280 characters a beginner can follow, e.g. "Daily and 15-minute
uptrend, 5-minute bar closed above ema9 (smoothed price); stop under swing low 497, target 506 (2x risk)".
