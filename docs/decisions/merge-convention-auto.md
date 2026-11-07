# Decision: auto-merge on review PASS (2026-11-07)

Supersedes DESIGN term 7's human-only merge gate. User-ratified 2026-11-07.

- A review card completing with verdict PASS authorizes dispatch to merge the PR.
- Merge only when PR head sha == the reviewed sha; if mergeable_state is dirty, a rebase chore lands first, tests re-green, then merge.
- No PASS, no merge. QA-only green is not sufficient; code review must be an independent lane (implementer ≠ reviewer rule unchanged).
- Human gates unchanged for: spending, irreversible/destructive ops, surge assignment.
- Close-out tracking: a card whose PR is not yet in main is not done (corrects the t_f36c45c3 miss where a rebase card closed with the PR unmerged, letting #7/#9 collide in flight).
