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
2. runs `claude -p` in this folder (tools: only `Skill`, to load the desk's skills), inside **one Claude conversation per New York trading day** (see below), with a JSON schema: `action` (buy / sell / close / wait), `size_r`, `stop`,
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
`instructions` is the hash of the exact text sent on that bar (with the plugin manifest and every skill file); `session` holds the id, bar number, the hash the session
started with and, on a new session, the intro text itself (`intro`, `intro_sha1`). Each bar also logs and journals
`context`: tokens the model read (input + cached input), output tokens, the window, `bar_cost_usd` (this bar, list price) and `session_cost_usd` (running total; records before 2026-10-23 15:00 have the total as `cost_usd`); a warning at 70% of the window.
If a stored session cannot be resumed the bar becomes `wait` and the next bar starts a new session with a recap.

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

## Guardrails for an interactive session

Four layers, each repeating the others so no single one has to be trusted:

1. **PreToolUse hook** (`desk/hook.py`, loaded by `.claude/settings.json`): runs before every MCP trade call and checks it
   against the lab's limits file (valid file, SPY listed, `size_r` in (0, 1], stop above 0, target above the stop or a
   reason, no STOP file). Exits (`close_position`, `cancel_order`) are never blocked. Fails closed: a crash blocks the call.
2. **Nobody but the person edits the guardrails**: `permissions.deny` rules on the limits file, the hook, the settings and
   `.mcp.json`, plus the same hook refusing Bash commands that name them (read-only `cat`/`grep`/`ls` are fine). The Bash check
   is a tripwire, not a sandbox.
3. **Inside the MCP server**: `gates.gate` and the lab's `Risk` (max shares, position value, trades per day, daily loss) in the Broker.
4. **On connect**: `connect_paper` refuses a non-local host, a non-paper port and any account not starting with `DU`.

5. **A pinned fingerprint of the limits file** (`desk/limits_pin.py`, `desk/limits.sha256`): before every `place_bracket` the
   MCP server, and before every `buy` the heartbeat (seatbelt `limits_pin`), compares the file's SHA-256 (a fingerprint of its
   exact bytes) with the committed pin and refuses the new entry on a mismatch. The model still sees the bar. `sell`, `close`,
   `wait`, the flatten-before-close seatbelt and all reads are unaffected. After a deliberate change a person runs `.venv/bin/python -m desk.limits_pin --update`
   and commits the new pin; the git diff is the record. Checking it by hand: `.venv/bin/python -m desk.limits_pin`.

Tests: `tests/test_guardrails.py` (layers 1-2), `tests/test_mcp_trade.py` (3), `tests/test_mcp_read.py` (4),
`tests/test_limits_pin.py` (5). The trader's own `claude -p` runs with no settings files and only the Skill tool, so none of
this applies to it.

### Finding: the limits file cannot be made tamper-proof on this machine

Tried 2026-10-22: the limits file was made `root:root`, mode 444. That stops editing it in place (`>>`, `sed -i`, an editor
saving), but its folder (`~/trading-lab/live/`) belongs to the desk's user, so any process running as that user can still
`mv` or `rm` the file and put another in its place; this was confirmed by hand. `chattr +i` (the immutable flag, which would
stop that) is refused on this machine with "Operation not permitted". So "no process running as the desk's user can change
the limits" is **not achievable inside this repo**. What the layers really give:

- **Prevent**: in-place edits (root ownership), and edits through Claude Code's tools (deny rules and the hook).
- **Detect and fail closed**: a replaced file, at the MCP server (layer 5), at the moment an order would be sent.
- **Not covered**: a process running as the desk's user that replaces the limits file *and* the pin, or edits the hook, the
  settings, `desk/limits_pin.py` or the lab's `live/risk.py`, or points `TRADING_LAB` at another copy of the lab. The repo's
  git history makes these visible afterwards, nothing more.
- **To close it for real** (outside this repo, a person's call): own the lab's `live/` folder by root (or move the limits file
  into a root-owned folder), or run the desk as a different OS user than the one that runs Claude Code, or use a filesystem
  that supports `chattr +i`.
