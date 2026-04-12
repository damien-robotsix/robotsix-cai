# PR Context Dossier
Refs: robotsix-cai/robotsix-cai#433

## Files touched
- `cai.py:3157` — added `_STALE_REVISING_HOURS = 1` constant after `_STALE_IN_PROGRESS_HOURS`
- `cai.py:3299–3305` — removed single `threshold` pre-computation; moved `lock_label` lookup to top of loop body; compute per-issue `ttl_hours` and `threshold` based on lock label
- `cai.py:3598` — changed report header from "Stale in-progress rollbacks" to "Stale lock rollbacks"

## Files read (not touched) that matter
- `cai.py` — `_rollback_stale_in_progress()` function (lines 3297–3354); constants block (~3154)

## Key symbols
- `_STALE_IN_PROGRESS_HOURS` (cai.py:3156) — existing 6-hour TTL for `:in-progress` locks (unchanged)
- `_STALE_REVISING_HOURS` (cai.py:3157) — new 1-hour TTL for `:revising` locks
- `_rollback_stale_in_progress` (cai.py:~3265) — function that detects and rolls back stale locks; logic for what rollback does was already correct

## Design decisions
- TTL set to 1 hour — conservative middle ground from issue's suggested 30–60 min; matches granularity of existing constant
- `lock_label` hoisted to top of loop — safe because it was only read after its former position; avoids duplicate lookup

## Out of scope / known gaps
- Manual removal of `:revising` label from issue #406 is a one-time operational step, not automated by this code change
- Function is still named `_rollback_stale_in_progress` — renaming was deliberately avoided to minimize diff

## Invariants this change relies on
- `issue.get("_lock_label", LABEL_IN_PROGRESS)` correctly reflects whether issue has `:revising` vs `:in-progress` label
- Rollback action for `:revising` (remove label only, leave `:pr-open`) is already correct in existing code
