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
