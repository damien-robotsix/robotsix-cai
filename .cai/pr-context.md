# PR Context Dossier
Refs: damien-robotsix/robotsix-cai#504

## Files touched
- cai_lib/__init__.py — new package init (empty)
- cai_lib/config.py — constants block (lines 148–223) + stale TTL constants
- cai_lib/logging_utils.py — 11 logging functions (lines 226–480)
- cai_lib/subprocess_utils.py — _run and _run_claude_p (lines 487–641)
- cai_lib/github.py — 8 GitHub helpers from two locations (lines 644–710, 1251–1319)
- cai_lib/cmd_fix.py — _parse_decomposition (lines 1589–1629)
- cai_lib/cmd_lifecycle.py — _rollback_stale_in_progress (lines 3927–4046)
- cai.py — replaced 8 extracted blocks with import statements (bottom-up)
- tests/test_rollback.py — updated patches from cai.* to cai_lib.cmd_lifecycle.*

## Files read (not touched) that matter
- tests/test_multistep.py — imports `from cai import _parse_decomposition`; still works via re-export
- tests/test_rollback.py — patches cai-level symbols; had to change to lifecycle-module patches

## Key symbols
- `_rollback_stale_in_progress` (cai_lib/cmd_lifecycle.py:18) — uses _gh_json/_set_labels from cai_lib.github and LOG_PATH from cai_lib.config
- `_parse_decomposition` (cai_lib/cmd_fix.py:7) — standalone, only stdlib re
- `_run_claude_p` (cai_lib/subprocess_utils.py:17) — depends on log_cost from logging_utils

## Design decisions
- Bottom-up edits to cai.py — preserves line number stability during sequential edits
- `from cai_lib.config import *` — wildcard import at module level re-exports all ~30 constants so callers in cai.py and tests don't break
- Kept `# noqa: E402` on all mid-file imports — they appear after non-import code (functions above them were replaced but comments remain)
- Rejected: explicit import list for config (too many symbols, wildcard is simpler)

## Out of scope / known gaps
- `_cleanup_orphaned_branches` (lines 3849–3924) not extracted — not in the issue scope
- `_fetch_previous_fix_attempts` not extracted — not in scope
- No refactoring of extracted logic — pure move only

## Revision 1 (2026-04-13)

### Rebase
- resolved: cai.py (conflict between HEAD _set_labels with check-workflows in _BASE_NAMESPACES and PR import line)

### Files touched this revision
- cai_lib/github.py:86 — added "check-workflows" to _BASE_NAMESPACES to match HEAD's update in _set_labels
- cai.py:3197 — removed redundant explicit `from cai_lib.config import _STALE_*` import (already covered by wildcard at line 156)

### Decisions this revision
- Kept PR's import-from-cai_lib approach; updated cai_lib/github.py to include the "check-workflows" namespace added by PR #497 on main
- Removed mid-file explicit _STALE_* import to address reviewer's redundant_code finding

### New gaps / deferred
- None

## Revision 2 (2026-04-13)

### Rebase
- resolved: cai.py (2 conflicts — constants/logging block vs imports, and _select_plan_target+github helpers vs import)

### Files touched this revision
- cai_lib/config.py:66 — added LABEL_HUMAN_SUBMITTED, LABEL_PLANNED, LABEL_PLAN_APPROVED (added to main in PRs #517/#518, missing from extracted module)
- cai.py:173 — conflict 1 resolved: replaced inline constants+logging functions with `from cai_lib.config import *` + `from cai_lib.logging_utils import ...`
- cai.py:744 — conflict 2 resolved: kept new `_select_plan_target` from main, replaced inline github helpers with `from cai_lib.github import ...`

### Decisions this revision
- `_select_plan_target` stays in cai.py — added by PR #521 on main, not in scope for this extraction PR
- New labels (LABEL_HUMAN_SUBMITTED, LABEL_PLANNED, LABEL_PLAN_APPROVED) added to cai_lib/config.py so `from cai_lib.config import *` wildcard re-exports them correctly

### New gaps / deferred
- Review comment "ok to merge" is an approval, no code change required

## Invariants this change relies on
- All extracted symbols remain accessible from `cai` module via import re-exports
- `cai_lib` dependency graph is acyclic: config → logging_utils → subprocess_utils → github → cmd_lifecycle
- `test_rollback.py` patches must target `cai_lib.cmd_lifecycle.*` since that's where the function's symbol lookups resolve
