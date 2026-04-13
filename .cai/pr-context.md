# PR Context Dossier
Refs: robotsix/robotsix-cai#510

## Files touched
- `cai_lib/cmd_lifecycle.py`:24 — added `_issue_has_label` to import from `cai_lib.github`
- `cai_lib/cmd_lifecycle.py`:148+ — added `_reconcile_fix`, `_reconcile_revise`, `_reconcile_refine`, `_reconcile_interrupted` after `_rollback_stale_in_progress`
- `cai_lib/__init__.py`:82 — added `_reconcile_interrupted` to import from `cmd_lifecycle`
- `cai_lib/__init__.py`:110 — added `"_reconcile_interrupted"` to `__all__`
- `tests/test_reconcile.py` — new test file with 11 test cases covering all branches

## Files read (not touched) that matter
- `cai_lib/cmd_lifecycle.py` — to locate `_rollback_stale_in_progress` end and existing import line
- `cai_lib/__init__.py` — to locate `_rollback_stale_in_progress` import and `__all__` list

## Key symbols
- `_reconcile_interrupted` (`cai_lib/cmd_lifecycle.py`) — dispatcher; returns not_started/partially_done/completed_externally
- `_reconcile_fix` (`cai_lib/cmd_lifecycle.py`) — checks PR list then matching-refs for fix actions
- `_reconcile_revise` (`cai_lib/cmd_lifecycle.py`) — checks LABEL_REVISING + PR list for revise actions
- `_reconcile_refine` (`cai_lib/cmd_lifecycle.py`) — checks issue body for `### Plan` marker
- `LABEL_REVISING` (`cai_lib/config.py`) — label constant used in `_reconcile_revise`

## Design decisions
- Helpers placed BEFORE dispatcher to avoid forward-reference confusion
- `git/matching-refs` API used for branch check (O(matches) not O(all-branches))
- No `cai.py` duplicate — Step 3 will import from `cai_lib` directly per #486 direction
- `_HANDLERS` dict evaluated at call time (not module load), so forward refs are safe either way

## Out of scope / known gaps
- Not hooked into `cmd_cycle` — that is Step 3 (#501)
- Does not read `cai-active.json` — caller passes explicit args
- `pr list --limit 50` is sufficient for this repo's scale; no pagination needed

## Invariants this change relies on
- `_issue_has_label` already exported from `cai_lib.github` (confirmed at `__init__.py`:77)
- `LABEL_REVISING` already imported in `cmd_lifecycle.py` from `cai_lib.config`
- `REPO` and `subprocess` already available in `cmd_lifecycle.py` scope
