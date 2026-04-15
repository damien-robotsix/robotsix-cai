# PR Context Dossier
Refs: robotsix/robotsix-cai#647

## Files touched
- `cai.py:940` — added `_apply_no_action_to_unlabeled_closed()` helper function before `cmd_audit()`
- `cai.py:986` — added Step 1f call to `_apply_no_action_to_unlabeled_closed()` in `cmd_audit()`
- `cai.py:1107` — added `no_action_applied` results to `deterministic_section`
- `.claude/agents/cai-audit.md` — added Note explaining the retroactive `:no-action` step and guardrail against re-raising `workflow_anomaly` for already-handled issues

## Files read (not touched) that matter
- `cai_lib/config.py` — defines LABEL_MERGED, LABEL_SOLVED, LABEL_NO_ACTION constants
- `cai.py:247` — `_fetch_closed_auto_improve_issues()` return shape (dict with `number`, `title`, `labels` as list of strings, `closedAt`)

## Key symbols
- `_apply_no_action_to_unlabeled_closed()` (`cai.py:940`) — new helper; fetches last 30 closed issues, filters to those lacking any terminal label, applies `:no-action`
- `_fetch_closed_auto_improve_issues()` (`cai.py:247`) — existing helper that returns closed issues with labels as plain string lists
- `_set_labels()` (`cai_lib/github.py`) — applies/removes labels via gh CLI
- `log_run()` (`cai_lib/logging_utils.py`) — writes structured log entry

## Design decisions
- Used `limit=30` (not 50) to match the plan spec and focus on recently closed issues
- Terminal label set: `{LABEL_MERGED, LABEL_SOLVED, LABEL_NO_ACTION}` — all three defined in `cai_lib/config.py`
- Inserted Step 1f before Step 2 so the closed-issues list seen by the LLM already has terminal labels applied
- `deterministic_section` entry added after `:pr-open` recovery block, consistent with existing pattern
- Rejected: applying labels only to a hardcoded exempt list — the structural fix (auto-apply on every audit run) is more robust

## Revision 1 (2026-04-15)

### Rebase
- clean

### Files touched this revision
- `.claude/agents/cai-audit.md:83` — added `auto-improve:solved` to the terminal states list in the "What to check" table row for `workflow_anomaly`

### Decisions this revision
- Minimal single-word addition to bring the table description in sync with the three-element `terminal_labels` set in `cai.py:945` and the notes at lines 144-146

### New gaps / deferred
- None

## Revision 2 (2026-04-15)

### Rebase
- clean

### Files touched this revision
- `cai.py:1172` — added `no_action_applied=len(no_action_applied)` to failure-case `log_run` call
- `cai.py:1188` — added `no_action_applied=len(no_action_applied)` to success-case `log_run` call

### Decisions this revision
- Reviewer correctly identified the count was absent from both `log_run` calls while all other deterministic step counts were logged; minimal two-line fix for observability parity

### New gaps / deferred
- None

## Out of scope / known gaps
- Does not backfill issues closed before this PR ships — only processes the last 30 on each audit run
- Does not check whether the closed issue was actually resolved; `:no-action` is used as "human manually closed, pipeline acknowledges"

## Invariants this change relies on
- `_fetch_closed_auto_improve_issues()` returns `labels` as a list of plain label name strings (not dicts)
- `LABEL_MERGED`, `LABEL_SOLVED`, `LABEL_NO_ACTION` are all importable via `from cai_lib.config import *` (already used in `cai.py`)
