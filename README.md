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
2. runs `claude -p` in this folder (tools: only `Skill`, to load the desk's skills) with a JSON schema: `action` (buy / sell / close / wait), `size_r`, `stop`,
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

## MCP server

`desk/mcp_server/` is an MCP (Model Context Protocol) server for an interactive Claude Code session in this folder
(the heartbeat's trader gets no tools and is unaffected). It runs over stdio (`python -m desk.mcp_server`, add
`--read-only` or `DESK_MCP_READONLY=1` to leave the trade tools out), is registered for this project in `.mcp.json`
(Claude Code asks you to approve it on first use) and connects to the **paper** Gateway as **client id 20**; the
lab's `connect_paper` refuses any non-paper host, port or account.

**All market data is delayed.** The paper account has no real-time subscription: bars arrive about 15-20 minutes after
they close, quotes are delayed too, and the entry checks use the close of the newest closed 5-minute bar. Every
price-bearing tool says so in its description and its result. Positions, orders and account values are as IB reports
them, but position values inside the account use delayed prices.

| Tool | What it does |
| --- | --- |
| `get_quote` | delayed bid / ask / last for a stock; falls back to the newest closed 5-minute bar |
| `get_bars` | closed OHLCV bars, any IB bar size and duration (newest 500 rows at most) |
| `get_indicators` | ema9/21, rsi14, atr14, Bollinger bands, vwap from the lab's `indicators` module, with a glossary |
| `get_positions`, `get_open_orders`, `get_account_summary` | what IB holds; each order / position says who owns it |
| `search_journal` | search the heartbeat's journal (`data/desk`) and this server's (`data/mcp`) |
| `place_bracket` | buy SPY with a stop and a target (a bracket); sized in R |
| `modify_order` | raise the stop (never down or wider) or move the target (reward-to-risk stays at least 2) |
| `cancel_order` | cancel an entry or a target; the protective stop of a position cannot be cancelled |
| `close_position` | sell the whole position at once with a marketable limit |

Trade tools are SPY-only and long-only, and every order goes through the heartbeat's own chain: `decision.parse`
(schema) -> `gates.gate` (STOP file, session, bar age, no adding, sizing in R, the lab's risk limits) -> the lab's
`Executor` / `Broker` / `Risk`. Beyond that:

- **Own orders only.** IB lets only the client that placed an order change it, so this server manages orders tagged
  `live:mcp:SPY` and refuses to trade while the heartbeat (client 8) has a position or resting order on SPY.
  The refusal works both ways: while this server has any order on SPY the heartbeat refuses every buy / sell / close
  (seatbelt `mcp_active`) and does not flatten those shares, because the server flattens its own, 5 minutes earlier.
- Its own `data/mcp/state.json` and `journal.jsonl`; it never writes the heartbeat's state.
- `size_r` is capped at 1R. A target is required unless `no_target_reason` says why.
- A **wall-clock seatbelt** (every 30 s) sells this server's position 15 minutes before the close (5 minutes before
  the heartbeat's own), sells anything carried overnight, and cancels a stop/target left with nothing to protect. A paper-guard
  or stuck-exit failure halts new entries (`halted` in `data/mcp/state.json`) until a person clears it; `close_position` still works.
- Not enforced in code, by the heartbeat or here (they are only in `CLAUDE.md` for the trader): no entries in the last
  hour, the 6-bar cooldown after a stop-out, stopping after 2 losses, about 3 entries a day. The code enforces the lab's
  limits (10 shares, 4 trades a day, $500 daily loss) and no entries in the last 10 minutes.

Tests: `tests/test_mcp_*.py` run the server in memory against a fake IB (`tests/mcp_fakes.py`).

## Status

Heartbeat built and dry-run on the paper Gateway. MCP server built and tested against a fake IB; one real read-only quote
checked against the paper Gateway. The constitution (the trader's system prompt) is `CLAUDE.md`; the detailed know-how is four skills in
`.claude/skills` (`market-read`, `entry-checklist`, `trade-management`, `post-trade-review`). The trader gets them through
`--plugin-dir` (`.claude-plugin/plugin.json` makes this folder a plugin that points at `.claude/skills`) and its only tool is
`Skill`; it loads them as `desk:<name>`. The instructions hash in the journal and ledger covers all of these files.
