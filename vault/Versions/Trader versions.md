---
type: report
date: 2026-10-24
sources: experiments/ledger.csv, git history, data/desk/journal.jsonl
---
# Trader versions: what changed and why

Paper trading only: simulated money. Written 2026-10-24 by hand from the ledger, git history and the journal. Nothing here
changes the desk's behaviour.

Terms: **trader version** = one row of `experiments/ledger.csv`. **Instructions hash** = a 12-character fingerprint (SHA-1) of
the exact text the trader reads: `CLAUDE.md`, the plugin manifest and every skill file (`desk/model.py`, `instructions_hash`).
Any edit to those files changes it. For Trader v1 the hash covered `CLAUDE.md` alone. A **bar** is the open, high, low, close
and volume of one 5-minute slice of SPY. A **skill** is a short how-to file the trader loads on request.

## The versions

| Version | Starts | Instructions hash | Commit | What changed | Why |
|---|---|---|---|---|---|
| (before v1) | 2026-10-22 | `3eff1ca76d95`, then `d221a07a2e5c` | `97df5d3` | The first heartbeat read its rules from `desk/instructions.md`. | The first working loop. **No ledger row** (see below). |
| Trader v1 | 2026-10-22 | `73a60f566379` | `fb55321` | The rules moved into `CLAUDE.md` as the trader's only instructions. | One constitution that is also versioned and hashed. |
| Trader v1.1 (day sessions) | 2026-10-23 | `73a60f566379` (unchanged) | `c82f570` | Code only: one Claude conversation per trading day instead of a fresh call per bar. | So the trader remembers its earlier bars. Results are not comparable with v1 without noting memory on/off. |
| Trader v2 | 2026-10-22 (row), first bar 2026-10-23 15:10Z | `a677c1ee2bcc` | `bc68b51` | `CLAUDE.md` trimmed; the detail moved into four skills (`market-read`, `entry-checklist`, `trade-management`, `post-trade-review`). | Shorter standing prompt, know-how loaded only when needed. Hash now covers the skills too. |
| Trader v2.1 (market-read always) | 2026-10-23 | `b66d6f25bd6d` | `2937d04` | Two sentences in `CLAUDE.md`: load `market-read` first on every flat bar; a failed safety check rules out a buy but does not end the bar. | The first 3 v2 bars loaded no skill and judged the data stale/thin at step 1. |
| Trader v2.1.1 (lessons key) | 2026-10-24 | `2392d5f3a4f5` | `75f0de7` | `CLAUDE.md` gained the required `lessons` key in the answer. | Lesson tracking needs to know which lessons each decision used. Ledger row added later (see below). |
| Trader v2.2 (proposal 0001) | 2026-10-24 | `3671b4756f4f` | `0245c85` | The `description:` line of `market-read` now says "Use FIRST on every bar where you hold nothing…". | v2.1 still loaded `market-read` on only 1 of 55 bars. Proposal `proposals/0001-market-read-load-first.md`. |

## Is every change to the instructions in the ledger? It was not; now one gap is closed
I recomputed the instructions hash at every commit that touched `CLAUDE.md`, `.claude/skills/` or the plugin manifest and
compared it with the ledger. The hashes of v1, v2, v2.1 and v2.2 match their commits exactly. Found, and what was done:

1. **Commit `75f0de7` (2026-10-24) changed the trader's instructions and had no ledger row.** It added the `lessons` key to
   `CLAUDE.md` (hash `2392d5f3a4f5`). **Fixed 2026-10-24:** row "Trader v2.1.1 (lessons key)" added. No bar ran under that hash.
   v2.2 includes it, so v2.2 is v2.1 + the `lessons` key + the `market-read` description; its note now says so.
2. **Every row had an empty `end_date`.** **Fixed:** v1, v1.1, v2 and v2.1 end 2026-10-23 (the last day with bars under their
   hash), v2.1.1 ends 2026-10-24, v2.2 is still running. Each changed row keeps its old text and says in the notes column that
   the end date was empty and where the new one comes from.
3. **Still open: journal bars ran on instructions the ledger does not list.** `d221a07a2e5c`: 4 bars, 2026-10-23 13:30 to
   13:45Z, which is `desk/instructions.md` as committed in `97df5d3`, the file from before v1. `3eff1ca76d95`: 1 bar,
   2026-10-22 19:55Z, and **no commit produces this hash**, so I cannot say what text it was (probably an uncommitted draft).
   All 5 bars were `wait`. These come before v1 and were left out of the ledger on purpose; they are only documented here.
4. **Still open: the v1 row's `start_date` is 2026-10-22**, but its first bar under hash `73a60f566379` is 2026-10-23 13:50Z (16
   bars). Left unchanged; the v1 note says so.

## What the journal shows per version

| Instructions hash | Bars | First bar (UTC) | Last bar (UTC) |
|---|---|---|---|
| `3eff1ca76d95` | 1 | 2026-10-22 19:55 | same |
| `d221a07a2e5c` | 4 | 2026-10-23 13:30 | 2026-10-23 13:45 |
| `73a60f566379` (v1, v1.1) | 16 | 2026-10-23 13:50 | 2026-10-23 15:05 |
| `a677c1ee2bcc` (v2) | 3 | 2026-10-23 15:10 | 2026-10-23 15:20 |
| `b66d6f25bd6d` (v2.1) | 55 | 2026-10-23 15:25 | 2026-10-23 19:55 |
| `3671b4756f4f` (v2.2) | 0 | not run yet | |

Every one of the 79 journal decisions was `wait`; no trade has closed, so **no version has a result in R yet**. Nothing here
shows that any version is better than another. The one measurable effect so far is skill loading: `market-read` loaded on 0 of 3
bars (v2) and 1 of 55 (v2.1). v2.2 is judged on that share after its first full session.

## Rolling back
Each applied proposal is one commit; `git revert <commit>` undoes it. Add an `end_date` to the ledger row by hand.

Links: [[Constitution risk rules]] · [[Lab risk limits]]
