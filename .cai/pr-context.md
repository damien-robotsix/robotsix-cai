# PR Context Dossier
Refs: robotsix-cai/robotsix-cai#519

## Files touched
- `cai.py:1955` — `origin_raised_label` fallback changed from `LABEL_REFINED` to `LABEL_PLAN_APPROVED`
- `cai.py:1958-1962` — lock step comment + `remove=` list updated from `LABEL_REFINED` to `LABEL_PLAN_APPROVED`
- `cai.py:1031-1047` — new `_get_plan_for_fix` helper added after `_extract_stored_plan`
- `cai.py:2072-2075` — inline plan extraction block replaced with `_get_plan_for_fix(issue, origin_raised_label)` call
- `cai.py:2113-2115` — plan header text updated from "produced by `cai plan` and stored on the issue" to "pre-computed by `cai plan` and approved by a human reviewer"

## Files read (not touched) that matter
- `cai.py:640-665` — `_select_fix_target` already uses `LABEL_PLAN_APPROVED` and updated docstring (steps 1-2 already done)
- `cai.py:1016-1028` — `_extract_stored_plan` (called by new `_get_plan_for_fix` helper)
- `cai.py:982-979` — `_run_plan_select_pipeline` definition (intentionally NOT removed per scope guardrails)

## Key symbols
- `_get_plan_for_fix` (`cai.py:1031`) — new helper that distinguishes `:requested` (no plan expected) from `:plan-approved` with missing plan (WARNING)
- `origin_raised_label` (`cai.py:1955`) — used throughout cmd_fix for label rollback; now defaults to `LABEL_PLAN_APPROVED`
- `LABEL_PLAN_APPROVED` — already imported via `from cai_lib.config import *`

## Design decisions
- Added `_get_plan_for_fix` wrapper instead of inlining — improves diagnosability (WARNING vs. info log) and keeps cmd_fix body clean
- `_run_plan_select_pipeline` function definition kept intact — only the call site in cmd_fix was replaced; function is still used in `cmd_plan`
- Rejected: deleting `_run_plan_select_pipeline` — still needed by `cmd_plan`

## Out of scope / known gaps
- `LABEL_REFINED` constant untouched — still used as intermediate state by `cmd_plan`
- `LABEL_REQUESTED` behavior unchanged — admin bypass still works
- `_select_fix_target` docstring/query already updated in a prior change; not touched here

## Invariants this change relies on
- `LABEL_PLAN_APPROVED` is already defined and imported via `cai_lib.config`
- `_extract_stored_plan` is defined before `_get_plan_for_fix` in the file
- Issues reaching `cmd_fix` with `:plan-approved` label have a stored plan block in their body (best-effort; WARNING logged if missing)

## Revision 1 (2026-04-13)

### Rebase
- clean

### Files touched this revision
- none — all documentation changes already committed at 241ad34

### Decisions this revision
- No edits made — review comment from @damien-robotsix (cai-review-docs) was already_addressed: the agent committed all described doc fixes at 241ad34 before posting the summary comment; wrapper incorrectly flagged the summary comment as unaddressed

### New gaps / deferred
- none

## Revision 2 (2026-04-13)

### Rebase
- clean

### Files touched this revision
- none

### Decisions this revision
- No edits made — review comment from @damien-robotsix (cai-review-pr pre-merge review) was already_addressed: all five claimed missing changes were verified present in the file — `_select_fix_target` uses `LABEL_PLAN_APPROVED` (line 665), `_get_plan_for_fix` helper exists (lines 1031-1046), `origin_raised_label` defaults to `LABEL_PLAN_APPROVED` (line 1955), lock step removes `LABEL_PLAN_APPROVED` (lines 1958-1962), plan header text updated (lines 2108-2115). Reviewer was reviewing an earlier commit state.

### New gaps / deferred
- none
