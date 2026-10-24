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
| `Weekly/` | One summary per week, `YYYY-Www` (e.g. `2026-W43`), written on Friday after the close: counters and a link to every trade note and daily note of the week. | The desk's note writer (see below). |
| `Lessons/` | One short note per lesson, a single sentence of "do this differently" with links to the trades that taught it. Only `status: active` ones reach the trader (see the memory budget below); retired ones live in `Lessons/Archive/`. | The desk's reviewer proposes one after each closed trade (`status: proposed`); the person decides what becomes a rule. |
| `Templates/` | [[Trade note]] and [[Daily note]]. Copy them, never fill them in place. | The person. |

## How a day flows
1. Open today's note in `Daily/` (made from the template). Read `Rules/` first.
2. Before looking for a trade, read the relevant `Playbook/` note.
3. After a trade ends, write its note in `Trades/`, link it from the daily note, and name the setup it used.
4. At the end of the day fill in the daily note's "How the day went". If a trade taught something, write a note in
   `Lessons/`. One trade is an anecdote, not a pattern: say "seen once" until it repeats.

## Notes the desk writes by itself
The heartbeat (`uv run -m desk`) fills `Trades/`, `Daily/` and `Weekly/` without anyone asking. It is a separate `claude -p`
call, not the trader, and it has no tools: it only returns sentences. The desk's code (`desk/scribe.py`) writes the files,
only inside those folders (`Trades/`, `Daily/`, `Weekly/` and the top level of `Lessons/`). Every number (prices, shares, result in R, counters) is computed by the code from the
journal and put in the note by the code; a sentence containing a number that is not in the journal facts is rejected.
- **When a trade ends** (the journal gets a `trade_closed` line): its note in `Trades/` from the [[Trade note]] template,
  linking the setup and the day, and the day's note lists it and updates its counters.
- **Right after that, the post-trade review** (a written look back at the finished trade). A second `claude -p` call is woken
  with the trade and the 5-minute bars since its entry and must load the trader's `post-trade-review` skill (the only tool
  it has; the call is rejected and repeated if the skill was not loaded). The code then writes the review into the trade note's
  `## Review` section, adds `reviewed:` to its front matter and writes **one tagged lesson** into `Lessons/`
  (`tags: [lesson, <what it is about>, <setup>]`, `status: proposed`, `seen: 1`; a lesson with the same title gets one more `seen`
  and a link instead of a copy; "nothing to change" writes no lesson). Only then is `trade_reviewed` journaled.
  **That record is a gate:** until the last closed trade has it, the desk refuses every new entry (selling is still allowed),
  and tells the trader why in the refused bar's verdict and in the snapshot's `entry_review_gate`. If Claude cannot produce a
  review it is retried every 2 minutes and the gate stays shut; Python never writes a review in Claude's place.
  A `proposed` lesson does not load on any bar, so it costs nothing of the memory budget until a person makes it `active`.
- **After 16:10 New York time**: "How the day went" in that day's note. A plan or lesson already written by hand is kept.
- **Friday after the close**: the week's summary in `Weekly/`, linking every trade note and daily note of the week.
- If a stop or target filled and the broker did not report the price, the exit price and result stay empty. Edit by hand.

## Memory budget: what the trader carries and what it looks up
The trader has no file tools, so it cannot browse this vault. It reads only what the desk puts in front of it, and every
token (a short word or a piece of a long one, about 3-4 characters) it reads on every bar costs time of the 120-second bar
budget and room in the conversation. Two tiers:

| Tier | What | Cost |
|---|---|---|
| **Every bar** | The constitution `CLAUDE.md`, the name and description of each desk skill, and the **memory brief**: one `summary` sentence for each lesson with `status: active`, sent in the first message of the day. | Counted, limit **6,000 tokens** in total. |
| **Lookup only** | Everything else: `Rules/` (already inside `CLAUDE.md`, so loading it again would only double it), `Playbook/`, `Trades/`, `Daily/`, `Weekly/`, `Market/`, the lessons' full text and `Lessons/Archive/`. A person, an interactive Claude session (the `vault` skill) or the note writer opens them when needed. | Zero per bar. |

The market snapshot is data, not memory, and is not counted. Measure with `uv run -m desk.memory`: it prints each item's
estimated tokens (characters / 3.5, a slight over-count on purpose), exits **1** above 6,000 and names the note to
summarise or move. It warns above 5,000 and whenever a seeded note is active. Run it after every change to `Lessons/`.

A lesson's `status` decides where it lives. Only the person sets `active` (Claude proposes, the person decides):

| `status` | Meaning | Loads every bar? | Folder |
|---|---|---|---|
| `proposed` | Written by Claude, not yet approved. | No | `Lessons/` |
| `active` | Approved. Needs `summary`, `seen` (times it was confirmed by a real trade) and `last_seen` (date). | **Yes** | `Lessons/` |
| `disputed` | Disagrees with another lesson and the tie is not resolved (see below). | No | `Lessons/` |
| `superseded` | Replaced by a better lesson (`superseded_by: [[...]]`). | No | `Lessons/Archive/` |
| `archived` | Retired to save room. | No | `Lessons/Archive/` |

### Summarising
- Each active lesson carries a front matter line `summary:` of **one sentence**, written for a beginner, explaining any
  term. The line the trader gets is `- title: summary` and must cost **at most 80 tokens (about 280 characters, title included)**. That sentence is all the trader sees; the note's body is the lookup tier.
- A lesson without a `summary` sends its whole body, so the script shows the cost and tells you to add one.
- Summarise when a lesson passes the 80-token limit or the script says to. A summary may drop examples but never a
  condition: "wait for the retest" must keep "after a breakout" or it will be applied everywhere.
- Older `Trades/` and `Daily/` notes are never loaded, so they are not summarised; the `Weekly/` note is their summary.
  When 10 or more lessons say nearly the same thing, merge them into one note, add up their `seen`, and keep the links
  to all their trades.

### Archiving
- When the total is over 6,000 (or above 5,000 and you are adding a lesson), archive in this order: first resolve
  disagreements (below), then summarise, then archive the lesson with the **lowest `seen`**, ties broken by the **oldest
  `last_seen`**. The script lists them in that order.
- To archive: move the note to `Lessons/Archive/`, set `status: archived` and add `archived: YYYY-MM-DD` with a reason.
  **Never delete**: the note keeps its links and can come back with a new `last_seen`.
- If the standing load is over budget with no lesson at all, `CLAUDE.md` or a skill description is too long. That is the
  person's file; the vault cannot fix it.
- A lesson seen only once is "seen once", not a pattern. It may stay active, but it is the first to go.

### When two lessons disagree
`CLAUDE.md` and the lab's limits always win over any lesson, and a lesson never loosens a risk rule. Between lessons:
1. **Check whether the conditions differ.** "Wait for the retest" and "buy the strong breakout" may both be right, one when
   volume is normal and one when it is high. If so, put the condition in each summary and keep both.
2. **Else compare the evidence.** If one lesson has at least 2 more real trades behind it (`seen`), it wins. If `seen`
   is within 1, the one whose `last_seen` is at least 7 days newer wins. Anything else is a tie.
3. **Record the result.** The loser gets `status: superseded`, `superseded_by: [[winner]]`, `resolved: YYYY-MM-DD` and a reason,
   and moves to `Lessons/Archive/`. The winner gets `replaces: [[loser]]`.
4. **On a tie, neither is trusted.** Both become `disputed` (not loaded) and the person decides. Until then the
   constitution alone governs. A lesson that is contradicted by a new real trade loses one `seen`; at `seen: 0` it is archived.

### Seeded notes
A note with `seeded: true` is made-up test data for the budget script, never a real trade or lesson. Its name starts
with `SEED`, it carries a warning banner, and it must be `archived` before a real run. The script warns if one is active.

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
