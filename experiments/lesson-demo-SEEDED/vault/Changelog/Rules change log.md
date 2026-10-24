---
type: changelog
---
# Rules change log

> [!danger] DEMO (seeded, made-up data)
> Every entry below comes from made-up trades; none is a real result.

Every change `desk/lessons.py --apply` makes to which lessons the desk keeps: promotions into the playbook, archivings and rollbacks. Newest at the bottom. The same change is a Git commit; `git log -- "vault/Changelog"` lists them. Rules: the docstring of `desk/lessons.py`.

## 2026-09-04 · PROMOTED to the playbook · [[SEED Wait for the retest after a breakout]]
- Why: 30 applied trades do clearly better than similar ones (the whole 95% interval is above 0).
- Evidence: applied: n=30, average +0.41R, SD 0.99, standard error 0.18. Similar, not applied: n=21, average -0.09R, SD 0.72, standard error 0.16. difference +0.50R, 95% interval +0.04R to +0.96R.
- Counted from: 2026-06-01. Source: experiments/lesson-demo-SEEDED/desk/journal.jsonl.
- DEMO (seeded, made-up data)

## 2026-10-23 · ROLLED BACK (removed from the playbook, archived) · [[SEED Wait for the retest after a breakout]]
- Why: 13 applied trades do clearly worse than similar ones (the whole 95% interval is below 0).
- Evidence: applied: n=13, average -0.87R, SD 0.89, standard error 0.25. Similar, not applied: n=14, average +0.08R, SD 0.74, standard error 0.20. difference -0.94R, 95% interval -1.56R to -0.37R.
- Counted from: 2026-09-05. Source: experiments/lesson-demo-SEEDED/desk/journal.jsonl.
- DEMO (seeded, made-up data)
