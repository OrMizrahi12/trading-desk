---
id: 0001
status: proposed
patch: 0001-market-read-load-first.patch
touches: .claude/skills/market-read/SKILL.md (the `description:` line only)
---
# 0001: make `market-read` impossible to skip on a flat bar

Paper trading only. Terms: a **skill** is a short how-to file the trader loads on request; its **description** is the one
line the trader sees on every bar before deciding whether to load it. A **bar** is the open, high, low, close and volume of
one 5-minute slice of SPY.

## Reason
`CLAUDE.md` (v2.1) already says "load market-read first on every flat bar". The trader mostly does not: it decides from
the raw snapshot, calls thin volume "incomplete data" and stays out without ever reading the skill that defines what
"incomplete" means. The description is the line it actually reads when choosing, so the instruction is moved there.

## Evidence
Lesson tracker (`uv run -m desk.lessons`): **none**. 0 closed trades, no active lesson, so it cannot support or refute
anything yet. This proposal is therefore based on the journal, not on a lesson, and is weak evidence:
- Trader v2.1 (instructions hash b66d6f25bd6d, 2026-10-23): `market-read` loaded on **1 of 55** bars (the first; skills=[] on the other 54).
- Trader v2 (a677c1ee2bcc): 0 of 3. Every one of the 58 decisions was `wait`.
- The reasons on skipped bars judge "thin volume, so the data looks incomplete" themselves, the exact failure v2.1 was meant to fix.

## Expected effect and how to judge it
Raises the share of flat bars that load `market-read` (journal field `skills`). It does not change when the desk trades;
the risk rules, gates and seatbelts are untouched. Judge after one full session: share of bars with `desk:market-read`
(was 2%). If it stays low, the description is not the lever and this is rolled back (revert the commit).
Caveat: one day of waits, no trades; this says nothing about profit.

## Cost
About +45 tokens in the standing load (limit 6,000; now 1,753).
