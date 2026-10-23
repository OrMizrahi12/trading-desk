---
type: readme
---
# Desk vault: the memory of the trading desk

This vault is the desk's written memory. **Paper trading only: simulated money, never a real account.** Nothing here is
financial advice. Plain Markdown files, tracked in Git with the rest of the repo, so every change is reviewable.

A few words used below:
- **bar** = the open, high, low, close and volume of one 5-minute slice of SPY (an index fund that follows the S&P 500).
- **R** = 1% of the account's value: the money lost if a trade's stop is hit. "+2R" means a profit of twice that.
- **stop** = a resting order that sells at your loss limit. **target** = a resting order that sells at your profit goal.
- **VWAP** = volume-weighted average price: the average price paid today, weighted by volume.

## The folders

| Folder | What lives there | Who writes it |
|---|---|---|
| `Rules/` | The risk limits, copied word for word from their source (see [[Constitution risk rules]], [[Lab risk limits]]). | **Nobody in this vault.** Read-only for Claude; a person updates it after the source changes. |
| `Playbook/` | One note per setup (a repeatable pattern worth trading): [[Opening range breakout]], [[VWAP reclaim]]. Entry, stop, target, when to skip. | Claude may add a setup or a dated "What we have seen" entry; the rules of a setup change only with the person's say-so. |
| `Market/` | Notes about the market itself: a regime (trending day, choppy day), an event day, data quirks (e.g. thin volume). Name them `YYYY-MM-DD topic`. | Claude. |
| `Trades/` | One note per trade, from the [[Trade note]] template: `YYYY-MM-DD HHMM SPY`. Written after the trade ends, as part of the review. | Claude. |
| `Daily/` | One note per trading day, `YYYY-MM-DD`, from the [[Daily note]] template: plan, how the day went, counters. | Claude. |
| `Lessons/` | One short note per lesson, a single sentence of "do this differently" with links to the trades that taught it. | Claude proposes; the person decides what becomes a rule. |
| `Templates/` | [[Trade note]] and [[Daily note]]. Copy them, never fill them in place. | The person. |

## How a day flows
1. Open today's note in `Daily/` (made from the template). Read `Rules/` first.
2. Before looking for a trade, read the relevant `Playbook/` note.
3. After a trade ends, write its note in `Trades/`, link it from the daily note, and name the setup it used.
4. At the end of the day fill in the daily note's "How the day went". If a trade taught something, write a note in
   `Lessons/`. One trade is an anecdote, not a pattern: say "seen once" until it repeats.

## Conventions
- Link with `[[Note name]]`. Every trade links its setup; every lesson links its trades; every daily note links its trades.
- Front matter (the `---` block on top) carries `type`, `date`, `setup`, `result_r` so that Obsidian's search and Bases
  can list and sum them.
- Times are New York time (the exchange's clock) unless marked UTC. The market is open 09:30-16:00.
- Never write a made-up number. If a price or result is unknown, leave the field empty.
- The vault never changes the desk's behaviour by itself. The rules that bind the trader live in `CLAUDE.md` and the lab's
  limits file; `Rules/` only mirrors them. If a note disagrees with those files, the files win.
- How Claude reads and writes this vault: the `vault` skill (`.claude/vault-skill/vault/SKILL.md`). It sits outside `.claude/skills/` on purpose: that folder is
  hashed into the trader's instructions, and the trader has no file tools. To use it in a session, symlink it:
  `ln -s "$PWD/.claude/vault-skill/vault" ~/.claude/skills/vault`.
