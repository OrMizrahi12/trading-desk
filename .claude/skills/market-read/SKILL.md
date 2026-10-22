---
name: market-read
description: Use on every bar before deciding anything new, to read the snapshot - daily trend, 15-minute trend, the latest 5-minute bar, and whether the data is fresh and complete enough to trade. Use it again whenever you doubt what the market is doing. It ends with a verdict (uptrend / downtrend / chop / unclear) and a data verdict (usable / not usable). It does not choose a stop, target or size (that is entry-checklist).
---

# Market read

Read top-down: slow clock first, fast clock last. Say each term in plain words when you use it.

## 0. Is the data usable? (if not, no buy; exits stay allowed)
- `session.bar_age_minutes` above `session.max_entry_age_minutes`: the bar is stale (too old to act on).
- Missing or empty bars, or `null` indicators in the latest rows: the snapshot is incomplete.
- Volume far below the recent bars, or several identical bars in a row: thin data or a stuck feed.
- `stop_file_present` true: the desk's kill switch is on, so no buys.
- `session.minutes_to_close` under 60: no new entries. `session.is_newest_closed_bar` false: old bar, no entry.
- Inside the first ~15 minutes after `session.open` (first three 5-minute bars): moves are wild, only watch.

## 1. Daily trend (`bars_daily`)
- Uptrend: last close above ema21 (exponential moving average: a smoothed price where newer days weigh more),
  ema9 above ema21, and recent daily lows higher than earlier lows.
- Downtrend: the mirror image. Since the desk is long only, a daily downtrend means `wait`.
- Chop: ema9 and ema21 tangled, closes criss-crossing them.
- Check `daily_source`: if the daily bars are partly built from today's 5-minute bars, today's candle is still forming.

## 2. 15-minute trend (`bars_15min`)
- Same test: close above ema9 and ema21, ema9 above ema21, higher swing lows (a swing low is a bar whose low is
  lower than the bars on both sides of it).
- The 15-minute trend must agree with the daily trend. If they disagree, the read is "unclear": `wait`.

## 3. The latest 5-minute bar (`bars_5min`, last row)
- Confirms the trend: closes above its open, closes above ema9 and ema21, and the close is in the top half of its range.
- rsi14 (relative strength index, 0-100 momentum gauge): about 50-65 is healthy in an uptrend; above ~75 is stretched
  (late, do not chase); below ~30 is beaten down (not a reason to buy by itself).
- atr14 (average true range: typical bar size in dollars): compare the latest bar's range (high minus low) to it.
  A range over about 2x atr14 is a spike bar - do not buy right after it.
- Bollinger bands (two standard deviations around a 20-bar average): a close at or above `bb_upper` is stretched.
- vwap (volume-weighted average price since the open, the day's fair price): above it favours buyers. Price far
  above both vwap and ema9 (roughly more than 1 atr14) means chasing.
- Volume: compare to the last ~10 bars. A confirming bar on rising volume is stronger than one on thin volume.

## 4. Verdict (one line, for yourself)
`daily <up/down/chop> | 15m <up/down/chop> | 5m <confirms/does not> | data <usable/not usable>`
- Only "up | up | confirms | usable" can lead to a trade: continue with entry-checklist.
- Anything else: `wait` (or manage the position you hold with trade-management).
