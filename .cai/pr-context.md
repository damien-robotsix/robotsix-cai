# PR Context Dossier
Refs: robotsix-cai/robotsix-cai#432

## Files touched
- `cai.py:2940` — removed `LABEL_MERGE_BLOCKED` from the `remove=` list in `_recover_stuck_rebase_prs()` so recovered issues retain `merge-blocked`
- `cai.py:2774` — initialized `pr_detail = None` before try block in `_select_revise_targets()`
- `cai.py:2778` — added `labels` to `--json` fields in per-PR `gh pr view` call
- `cai.py:2786-2796` — added secondary per-PR `needs-human-review` guard using fresh `gh pr view` label data

## Files read (not touched) that matter
- `cai.py:2707-2840` — `_select_revise_targets()` full body — primary revise guard logic
- `cai.py:2843-2947` — `_recover_stuck_rebase_prs()` full body — recovery logic that was stripping `merge-blocked`
- `cai.py:2950-2965` — `cmd_revise()` — recovery runs before target selection, explaining the ordering bug

## Key symbols
- `_recover_stuck_rebase_prs` (`cai.py:2843`) — ran first in cmd_revise; was stripping merge-blocked, nullifying the guard
- `_select_revise_targets` (`cai.py:2707`) — primary guard; now has secondary fresh-label check
- `LABEL_MERGE_BLOCKED` (`cai.py:202`) — `"merge-blocked"` — issue-level block label
- `LABEL_PR_NEEDS_HUMAN` (`cai.py:209`) — `"needs-human-review"` — PR-level block label

## Design decisions
- Preserve `merge-blocked` in `_recover_stuck_rebase_prs` — intentional; cmd_merge explicitly evaluates merge-blocked PRs so it will re-evaluate any fresh PR and remove the label if appropriate
- Add secondary per-PR label check after per-PR `gh pr view` — defense against stale `gh pr list` data for recently-labelled PRs
- Guard placement: secondary check runs before `if not last_commit_date` so stale-label PRs are skipped with an explanatory log message rather than silently
- Rejected: fixing only one of the two root causes — both are needed for defense-in-depth

## Out of scope / known gaps
- `cmd_fix` does not check `merge-blocked` before picking up `:refined` issues — this is intentional per the plan (cmd_fix should be able to re-attempt)
- Did not add `merge-blocked` check to the secondary per-PR block (only checking `needs-human-review` there) because `merge-blocked` is an issue label, not a PR label

## Invariants this change relies on
- `cmd_merge` does NOT skip issues/PRs that have `merge-blocked` — it evaluates them and removes the label on success (confirmed at line ~6226 and ~6552)
- `_recover_stuck_rebase_prs` runs before `_select_revise_targets` in `cmd_revise` — ordering at lines 2956 and 2963
