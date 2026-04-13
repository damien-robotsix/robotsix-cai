# PR Context Dossier
Refs: robotsix/robotsix-cai#534

## Files touched
- `cai.py:194` — expanded inline github import into multi-symbol form, added cmd_lifecycle and cmd_fix imports
- `cai.py:808` — deleted inline `from cai_lib.github import _set_labels, ...` (was between two functions)
- `cai.py:1320` — deleted inline `from cai_lib.cmd_fix import _parse_decomposition` (was inside Multi-step section)
- `cai.py:3755` — deleted inline `from cai_lib.cmd_lifecycle import _rollback_stale_in_progress` (was between two functions)

## Files read (not touched) that matter
- `cai_lib/cmd_lifecycle.py` — verified no circular import back into `cai`
- `cai_lib/cmd_fix.py` — verified no circular import back into `cai`

## Key symbols
- `_set_labels`, `_issue_has_label`, `_build_issue_block`, `_build_fix_user_message` (`cai_lib/github.py`) — moved from inline import at old line 808 to top-level
- `_parse_decomposition` (`cai_lib/cmd_fix.py`) — moved from inline import at old line 1320 to top-level
- `_rollback_stale_in_progress` (`cai_lib/cmd_lifecycle.py`) — moved from inline import at old line 3755 to top-level

## Design decisions
- Edits applied bottom-to-top to avoid line-number drift between changes
- Used multi-line parenthesized import for the github block to keep line length reasonable
- Rejected: restructuring all cai_lib imports into a single block — out of scope per plan selection

## Out of scope / known gaps
- Removing duplicate symbol *definitions* from cai.py (constants, functions like `_run`, `log_run`, etc.) — that is a separate follow-on
- Moving `cmd_*` functions into `cai_lib` submodules — step 3 follow-on

## Invariants this change relies on
- `cai_lib.cmd_lifecycle` and `cai_lib.cmd_fix` do not import from the top-level `cai` module (verified)
- All three symbols were already imported via these same `from cai_lib.X import Y` statements; this is a pure location move with no semantic change
