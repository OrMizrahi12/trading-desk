# trading-desk

An AI trading desk for **paper trading only**, built for education.

- An AI model acts as the decision-maker on a simulated (paper) account.
- Markets: US stocks and ETFs, using 5-minute bars (one price summary per 5 minutes).
- No real money, no live brokerage account, no financial advice.

## Experiments

Every experiment run is recorded in `experiments/ledger.csv` (version, model, dates,
instructions file hash, symbols, bar size, risk limits, notes) so results can be
reproduced and compared.

## Status

## Heartbeat

`uv run -m desk [--once] [--dry-run]` connects to the **paper** Gateway only (the lab refuses anything else) and,
for every closed 5-minute SPY bar:

1. polls the lab's bar feed (`~/trading-lab/live/feed.py`) and builds a snapshot: 5-minute, 15-minute and daily bars
   with indicators, positions, open orders, account, session clock, the STOP-file flag and the last journal entries
   (written to `data/desk/snapshots/`);
2. runs `claude -p` in this folder with a JSON schema: `action` (buy / sell / close / wait), `size_r`, `stop`,
   `target`, `reason`. **R** = 1% of the account's value, the money lost if the stop is hit;
3. validates the answer (anything invalid or missing becomes `wait`), then the gates: STOP file (blocks buys; exits
   still pass), bar age, session time, no adding to a position, lab risk limits;
4. executes through the lab's `Executor` / `Broker` / `Risk` (a bracket = entry + stop + target);
5. appends everything to `data/desk/journal.jsonl`.

Seatbelts (`desk/seatbelts.py`) are hard rules in code that always win over the model: the STOP file, the lab's
risk limits, the entry window, a **wall-clock flatten 10 minutes before the close** (checked on every poll, before
the new bars are handled, even if the feed is down) and a sale of any position carried from an earlier day
(never overnight). This is Python enforcing the rules, not trading. Every time one acts it is logged
(`SEATBELT ...`) and journaled (`seatbelt` records, or `seatbelts` on the bar's record). If a sale cannot be
confirmed, or no price is known, the desk halts for a person to look.

Each bar has a 120-second budget. Python never filters or decides: the model sees every bar; Python can only
refuse an unsafe decision. `sell` only reduces a long position (no shorting). Tests: `uv run pytest` (fake model,
fake IB, no network).

## Status

Heartbeat built and dry-run on the paper Gateway. Strategy instructions: `desk/instructions.md`.
