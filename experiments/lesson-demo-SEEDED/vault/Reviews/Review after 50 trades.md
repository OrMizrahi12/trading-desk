---
type: review
trades: 50
first_day: 2026-06-01
last_day: 2026-09-02
seeded: true
---
# Review after 50 trades

> [!danger] DEMO (seeded, made-up data)
> Every trade below is made up. Nothing here says anything about the real desk.

The first 50 closed trades of the desk, 2026-06-01 to 2026-09-02, split three ways. **R** = 1% of the account, lost if a trade's stop (a resting order that sells at the loss limit) is hit; a result of +2R means the trade made twice what it risked. **Average R** = the mean result. The **95% interval** is the range the true value probably lies in, found by re-drawing the same trades 10,000 times (bootstrap); a wide interval means the number is not known well. **Versus the other trades** = this group's average R minus the average R of all other trades in the block. All numbers are computed by `desk/review.py` from the journal and the trade notes.

50 trades have a result in R; 0 have none recorded and are left out.

## All trades
| Group | Trades | Win rate (95% range) | Average R | 95% interval of the average | Versus the other trades | Reading |
|---|---|---|---|---|---|---|
| all trades | 50 | 54% (40% to 67%) | +0.22R | -0.03R to +0.47R | n/a | the baseline the groups below are compared with |

## Setup
| Group | Trades | Win rate (95% range) | Average R | 95% interval of the average | Versus the other trades (difference, 95% interval) | Reading |
|---|---|---|---|---|---|---|
| opening-range-breakout | 24 | 54% (35% to 72%) | +0.23R | -0.14R to +0.62R | +0.03R (-0.46R to +0.53R) | no clear difference |
| vwap-reclaim | 26 | 54% (35% to 71%) | +0.20R | -0.15R to +0.54R | -0.03R (-0.54R to +0.46R) | no clear difference |

## Time of day
| Group | Trades | Win rate (95% range) | Average R | 95% interval of the average | Versus the other trades (difference, 95% interval) | Reading |
|---|---|---|---|---|---|---|
| morning (before 11:00) | 15 | 53% (30% to 75%) | +0.20R | -0.36R to +0.75R | -0.02R (-0.64R to +0.61R) | no clear difference |
| midday (11:00 to 13:00) | 22 | 55% (35% to 73%) | +0.13R | -0.20R to +0.48R | -0.16R (-0.65R to +0.34R) | no clear difference |
| afternoon (from 13:00) | 13 | 54% (29% to 77%) | +0.39R | -0.03R to +0.83R | +0.23R (-0.29R to +0.76R) | no clear difference |

## Market regime (15-minute trend at entry)
| Group | Trades | Win rate (95% range) | Average R | 95% interval of the average | Versus the other trades (difference, 95% interval) | Reading |
|---|---|---|---|---|---|---|
| down | 14 | 50% (27% to 73%) | -0.05R | -0.44R to +0.37R | -0.37R (-0.86R to +0.15R) | no clear difference |
| mixed | 15 | 53% (30% to 75%) | +0.45R | -0.01R to +0.94R | +0.33R (-0.22R to +0.90R) | no clear difference |
| up | 21 | 57% (37% to 76%) | +0.23R | -0.16R to +0.61R | +0.02R (-0.50R to +0.52R) | no clear difference |

## What the numbers can and cannot say
- A group needs 10 trades before it is judged; below that the average is mostly luck. 0 of the 8 groups here are below that.
- 8 groups were judged. Chance alone flags about 1 in 20 groups as clearly better or worse, so about 0.4 of them would be flagged even if nothing mattered.
- Flagged in this block: none. No group is clearly different from the rest.
- The three splits overlap (each trade is in one group of each split), so they are not independent evidence.
- One block of trades is a sample; a setup, hour or regime is a rule only when later blocks agree.
- Regime is the 15-minute trend at entry, worked out by the desk's code; trades from before it was recorded say `not recorded`. Setup comes from the trade note; a trade without a note says `unknown`.
