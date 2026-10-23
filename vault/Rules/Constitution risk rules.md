---
type: rules
readonly: true
source: CLAUDE.md (section "Risk rules (hard, always)")
copied: 2026-10-23
---
> [!warning] READ-ONLY for Claude
> This note is a copy. Claude must not edit, reword, add to or delete it. To change a rule, a person edits the source
> (`CLAUDE.md`) and then refreshes this copy. If this copy and the source disagree, **the source wins**.

# Constitution risk rules (copy)

Companion: [[Lab risk limits]] (the numeric limits enforced in code). Setups that must obey these: [[Opening range breakout]], [[VWAP reclaim]].

Terms: **R** = 1% of net liquidation (the total account value) lost if the stop is hit. **Stop** = a resting order that
sells at your loss limit. **Target** = a resting order that sells at your profit goal. **Bar** = open, high, low, close
and volume of one 5-minute slice.

- R = 1% of net liquidation lost if the stop is hit. `size_r` is the size in R: typically 0.5, never above 1.
- Every buy has a stop and a target at least 2x the stop distance away (`null` target only with a reason). No stop level, no trade.
- Never move a stop down or widen it; if the idea is wrong, `close`. Never average down. The desk refuses any add.
- Long only. `sell` only reduces an existing long.
- Be flat 10 minutes before the close, never overnight. No new entries when `minutes_to_close` is under 60.
- At most about 3 entries a day. After a stop-out wait 6 bars (30 minutes). After 2 losing trades, no more entries today.
- No entries in the first ~15 minutes, when the data is stale (`bar_age_minutes` above `max_entry_age_minutes`),
  incomplete, or thin, or when the STOP file flag is on. Exits are always allowed.
- Do not chase spikes. Default to `wait`: patient and mostly flat is the style. When in doubt, `wait`.
