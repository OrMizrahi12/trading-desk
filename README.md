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
2. runs `claude -p` in this folder, inside **one Claude conversation per New York trading day** (see below), with a JSON schema: `action` (buy / sell / close / wait), `size_r`, `stop`,
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

## One conversation per day

The first bar of a day starts a Claude session (`--session-id`, id kept in `data/desk/state.json`); every later bar
continues it (`--resume`), so the trader remembers its own reasoning from earlier today. The first message holds
today's date, a short summary of the previous trading day built from the journal (counts, trades, seatbelts, the last
decision) and, if the session was lost or restarted mid-day, a recap of today so far. A new session also starts when
`CLAUDE.md` changes, so a conversation never mixes two sets of instructions.

Later bars send a **delta**, not the whole snapshot (a full one is ~30k tokens, 78 of them would overflow the
window): the new 5-minute bars (and 15-minute bars) with indicators, today's daily bar, session clock, positions, open
orders, fills, account, STOP flag, limits, and the journal entries the session has not seen yet (the desk's verdict and
result for its last decision). The first bar of a session, a session rebuilt after a loss, and a bar whose previous bar
is older than the snapshot's history get the full snapshot. The journal says which (`input`: full/delta, `input_sha1`,
`input_file`, `input_bytes`); the full snapshot is still built, hashed and saved for every bar.

If the context passes 80% of the window the desk starts a new session itself (with a recap of today), so claude never
has to compact the conversation on its own: a summary would replace the trader's exact earlier reasoning.

Which instructions the trader saw: `claude` keeps the system prompt of a session's first request on resume unless told
not to, so the desk passes `--system-prompt-snapshot off` and sends `CLAUDE.md` on every bar. The journal's
`instructions` is the hash of the exact text sent on that bar; `session` holds the id, bar number, the hash the session
started with and, on a new session, the intro text itself (`intro`, `intro_sha1`). Each bar also logs and journals
`context`: tokens the model read (input + cached input), output tokens, cost and the window; a warning at 70% of the window.
If a stored session cannot be resumed the bar becomes `wait` and the next bar starts a new session with a recap.

Each bar has a 120-second budget. Python never filters or decides: the model sees every bar; Python can only
refuse an unsafe decision. `sell` only reduces a long position (no shorting). Tests: `uv run pytest` (fake model,
fake IB, no network).

## Status

Heartbeat built and dry-run on the paper Gateway. The constitution (the only instructions the trader reads) is `CLAUDE.md`.
