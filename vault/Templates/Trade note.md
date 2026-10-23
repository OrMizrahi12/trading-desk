---
type: trade
date: {{date:YYYY-MM-DD}}
symbol: SPY
setup: 
entry_time: 
entry_price: 
stop: 
target: 
size_r: 
exit_time: 
exit_price: 
exit_type: 
result_r: 
classification: 
---
# {{date:YYYY-MM-DD}} SPY trade

Setup: [[Playbook/]] · Day: [[Daily/{{date:YYYY-MM-DD}}]] · Rules: [[Constitution risk rules]]

Terms: **R** = 1% of the account lost if the stop is hit. **Stop** = resting order that sells at the loss limit.
**Target** = resting order that sells at the profit goal. `result_r` = (exit - entry) / (entry - stop).
`exit_type` = stop / target / close / sell / seatbelt (forced sale by the desk's code).
`classification` = good trade good result / good trade bad result / bad trade good result / bad trade bad result.

## Why I bought (one sentence a beginner can follow)

## Checklist
- [ ] Data fresh and complete; volume not thin
- [ ] Not in the first ~15 minutes; `minutes_to_close` >= 60
- [ ] Stop level named before the buy; target >= 2x the stop distance
- [ ] Size 0.5R typical, never above 1R
- [ ] Cooldown clear (6 bars after a stop-out); fewer than 2 losses and 3 entries today

## What happened

## Review
- Classification:
- One lesson (one sentence, or "nothing to change"): link it as [[Lessons/]]
