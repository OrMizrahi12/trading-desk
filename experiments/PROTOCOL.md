# Forward-test protocol: Trader v2.2 + settled bars

Paper trading only. Written on Saturday 2026-10-24, **before any bar of the test period exists and before any result of this
version was looked at.** The numbers below are fixed; `desk/forward_report.py` holds the same numbers and
`tests/test_forward_report.py` fails if the two disagree. The only proof that this file was not edited afterwards is git:
the report prints this file's commits and warns if there is more than one.

A **forward test** judges a system only on days that come after the rules were written down, so nothing can be tuned to
what already happened. A **bar** is the open, high, low, close and volume of one 5-minute slice. A **stop** is a resting
order that sells at the loss limit; a **target** is a resting order that sells at the profit goal. **R** is the money lost
if the stop is hit, so a trade's result in R is (exit - entry) / (entry - stop), costs included: a stop-out is about -1R,
a target at twice the stop distance about +2R. All arms are counted in R because the lab caps the paper position at a few
shares (README: 10 shares), which makes dollar returns of the account meaningless.

## Question

Does the trader's choice of entries and exits beat what random entries with the same frequency and the same risk would
have produced? One question, one test. Everything else in this file is context.

## What is tested

| Item | Value |
| --- | --- |
| Version | **Trader v2.2 + settled bars** (last row of `experiments/ledger.csv`) |
| Instructions hash | `3671b4756f4f` (sha1[:12] over CLAUDE.md, plugin manifest and all skill files, as in the journal's `instructions`; verified equal to the code at commit `3deaaf6`) |
| Model | `claude-cli-default`, as in the ledger (not pinned: the CLI's default can change under us; the report cannot detect that, so a model change announced by the CLI ends the test like a hash change) |
| Mode | plain heartbeat only: no `--team`, `--regime-gate` or `--orb-veto` (a bar record that has a `team`, `regime_gate` or `orb_veto` key is not counted) |
| Data | decisions only on settled bars (16+ minutes after the bar's end), the fix this version introduced |
| Symbol | SPY only, long only, 5-minute bars |
| Period | **2026-10-26 to 2027-04-19**: the first 120 NYSE trading days from Monday 2026-10-26 (holidays out) |
| Minimum | **100 scored closed trades** (a scored trade has a known exit price and result in R) |
| Stop | the first of: 100 scored trades, or the last day of the period |

Trades of earlier versions (v1, v1.1, v2, v2.1, and any bar before 2026-10-26) are not in this test, and their results were
not used to choose any number here. If the instructions hash changes (any edit to CLAUDE.md, the plugin manifest or a
skill), or the mode changes, the test **ends on that day**: trades so far are reported as a partial result of this version
and are not added to the next version's count. A new version gets a new ledger row and a new protocol.

## What counts

- **A day counts** if at least 95% of its entry-window bars (bars whose close is 15 or more minutes after the open and 60
  or more minutes before the close, the window in which the desk allows an entry) are both in the saved 5-minute data and
  have a plain-mode journal record under this hash. Other days are dropped from **every** arm, so all arms see the same days.
  Dropped days are listed in the report.
- **A trade counts** if its buy bar is on a counted day, under this hash, in plain mode. Its result is the `result_r` of the
  journal's `trade_closed` record (from the broker's fills). A trade with no known result is reported and not guessed; if more
  than 10% of the trades have none, the verdict is inconclusive.
- **A day without a trade counts as 0** in the daily series. A patient strategy is judged on all days, not only those it traded.

## Metrics

1. **Primary:** average result per trade in R, with a 95% bootstrap range (the same trades re-drawn with replacement 5,000 times).
2. **Daily Sharpe ratio** per arm: average daily result divided by how much it bounces around, times sqrt(252) (trading days
   in a year), with a 95% range from re-drawing days. This is the unit-free number that lets the arms be set side by side.
3. Total R, number of trades, win rate and worst day are shown for the record; they carry no verdict.

## Baselines

Same days, same bars (the newest version of each bar from the saved snapshots), same costs (the lab's `CostModel`: half a
spread plus 1 cent slippage on market fills, 1 cent commission per share each way).

1. **Random entries matched on frequency and risk.** 2,000 replicates (seed 20261026). In each replicate, on every counted day,
   as many entries as the trader made that day, at bars drawn uniformly from that day's entry window. The i-th random trade of a
   day takes the stop distance (as a share of the price) and the target multiple of the trader's i-th trade of that day (no target
   if the trader had none), so frequency per day and risk per trade are identical by construction. Filled at the next bar's open,
   stop checked first, flat 10 minutes before the close. A draw falling inside the previous random trade is dropped and counted; if more than 25% of the draws are dropped the report says the frequency match is degraded.
2. **Buy-and-hold SPY.** Buy at each counted day's open and sell at its close (flat overnight, like the desk), and also one buy at
   the first open and one sale at the last close of the period. Percent, no costs; it has no stop, so it has no R and only its Sharpe is compared.
3. **ORB on the same days.** The lab's `opening_range_breakout` with its default settings (5-minute opening range, buy on a close
   above its high, stop = range low, no target, one signal a day), run by the lab's own backtest engine on the counted days.

## Verdict rule (applied once, at the stop)

Let p be the share of random replicates whose average R is at least the trader's (plus 1 for the trader's own result, divided
by replicates + 1). Then:

- **Better than luck:** p < 0.05 **and** at least 100 scored trades **and** at most 10% unscored.
- **Not distinguishable from luck:** at least 100 scored trades, p >= 0.05. This is not "the trader has no edge"; see the table below.
- **Inconclusive:** fewer than 100 scored trades at the end of the period, or too many unscored trades. No performance claim may be made.

ORB and buy-and-hold are context, not tests. "Beats ORB" or "beats the market" may not be claimed unless the two Sharpe ranges do
not overlap, and even then it is only a hypothesis for the next experiment.

## How many trades before a Sharpe can be told from luck

Test: one-sided, 5% chance of fooling ourselves when there is no edge. Power 80% = the chance of seeing a real edge when it exists.
The per-trade Sharpe is the average result divided by its spread; its estimate wobbles by 1/sqrt(trades). Trades needed =
((1.645 + 0.842) / per-trade Sharpe)^2 (for the coin-flip chance, 0.842 becomes 0). "Yearly" assumes one trade per trading day
(252 a year); the trades needed depend only on the per-trade Sharpe, so a desk that trades less often just takes longer.
`uv run -m desk.forward_report --table` prints these tables; a test checks them by simulation.

| True Sharpe (yearly) | Same per trade | Trades for a 50% chance to see it | Trades for an 80% chance | Months at 1 trade a day (80%) |
| --- | --- | --- | --- | --- |
| 0.5 | 0.031 | 2,728 | 6,233 | 296.8 |
| 1.0 | 0.063 | 682 | 1,559 | 74.2 |
| 1.5 | 0.094 | 304 | 693 | 33.0 |
| 2.0 | 0.126 | 171 | 390 | 18.6 |
| 3.0 | 0.189 | 76 | 174 | 8.3 |
| 4.0 | 0.252 | 43 | 98 | 4.7 |

| Trades so far | Yearly Sharpe needed to beat luck (5% one-sided) |
| --- | --- |
| 10 | 8.26 |
| 20 | 5.84 |
| 30 | 4.77 |
| 50 | 3.69 |
| 100 | 2.61 |
| 200 | 1.85 |
| 500 | 1.17 |
| 1,000 | 0.83 |

How to read it: with the 100 trades this protocol asks for, only a true yearly Sharpe near 4 would be seen 80% of the time, and
a result must reach about 2.6 to beat luck at all. Yearly Sharpe 1 (a good strategy by common standards) would need over
1,500 trades. **So this test can confirm a very strong edge or a very bad system; it cannot confirm a modest one.** That is
the reason for writing it down now: a modest-looking positive number after 100 trades is what luck produces all the time.
The table assumes roughly bell-shaped results; fat tails (rare big losses) make the true bar higher, never lower.

## Rules against fooling ourselves

- **Sealed report.** `uv run -m desk.forward_report` prints only data quality (days counted, trades, coverage) until the stop rule
  is reached. `--unseal` prints everything but heads it EXPLORATORY; those numbers must not be quoted as the result.
- **No peeking-based stopping.** The test is not stopped, extended, or its numbers changed because of how the results look. The
  only reasons to stop early are a safety event (a seatbelt halt, a paper-guard failure) or a hash/mode change as above.
- **No second chances on the primary test.** One comparison carries a verdict. Anything else found in the data is a hypothesis
  for a later protocol, not a finding.
- **Lessons and reviews keep running** (they are part of this version), but this protocol's test does not slice trades by lesson,
  setup, time of day or regime. The 50-trade review does that, with its own rules.

## Known biases (all against the trader or neutral, none in its favour)

- The baselines fill at the next bar's open on final data; the trader decides 16-21 minutes after the bar closes (delayed data) and
  fills at the then-current price. The baselines have no delay, so they are slightly generous to themselves.
- The random arm copies the trader's own stop distances and targets, so the test measures **timing of entries and exits**, not the
  choice of risk geometry.
- The trader's fills are the paper broker's; the baselines' fills are modelled. Paper fills are optimistic about slippage; the model
  charges 1 cent.
- Trades of one day are not independent (same market); the daily Sharpe and its day-resampled range respect that, the per-trade
  average does not fully. The sealed verdict uses the random arm, which has the same dependence, so the comparison stays fair.

## How to run

`uv run -m desk.forward_report` (sealed), `--unseal` (exploratory), `--write` (saves `experiments/forward-report.md` once final),
`--table` (the tables above). Tests: `uv run pytest tests/test_forward_report.py`.
