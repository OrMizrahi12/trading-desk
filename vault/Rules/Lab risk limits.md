---
type: rules
readonly: true
source: ~/trading-lab/live/risk_limits.yaml
source_sha256: 27db5018fb94d18e7f2ed4345eb6b74447a1f89825cb952530f551d0eabe13ce
copied: 2026-10-23
---
> [!warning] READ-ONLY for Claude
> This note is a copy. Claude must not edit, reword, add to or delete it. A person changes the limits in the source file
> and re-pins it (`python -m desk.limits_pin --update`), then refreshes this copy. If this copy and the source disagree,
> **the source wins**. `source_sha256` is the fingerprint of the file as copied: if `desk/limits.sha256` shows a different
> value, this copy is out of date.

# Lab risk limits (copy)

Companion: [[Constitution risk rules]]. These are checked in code before every order leaves the program, whatever the
trader asks for. Sells and cancels are never refused.

```yaml
# Risk limits for the live paper engine. Read by live/risk.py; checked in Broker before EVERY order leaves the program,
# whatever the strategy asks for. Paper trading only.
#
# The paper account is about $1,000,000. Sizing at 1% risk per trade would allow positions of hundreds of
# thousands of dollars (1% of $1M = $10,000 at risk; with a 1% stop that is a $1,000,000 position). These limits
# are deliberately far below that: a position is capped at about 0.8% of the account.
#
# A symbol that is not listed here cannot be bought at all. Sells and cancels are never refused.
symbols:
  SPY: {max_shares: 10, max_position_value: 8000}   # dollars, measured at the order's limit price
  QQQ: {max_shares: 10, max_position_value: 8000}
max_trades_per_day: 4       # entries (distinct entry refs) bought or resting at IB today, all symbols
daily_loss_limit: 500       # dollars: no new buys once net liquidation is this far under the day's first reading
```

In plain words:
- **max_shares: 10** for SPY: never more than 10 shares at once. **max_position_value: 8000**: never more than $8,000 of SPY at once.
- **max_trades_per_day: 4**: a hard ceiling in code. The constitution's "about 3 entries a day" is the tighter, working limit.
- **daily_loss_limit: 500**: **net liquidation** (total account value) falling $500 under the day's first reading stops all new buys.