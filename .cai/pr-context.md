# PR Context — Issue #406: Pre-Screen Gate Before Plan-Select Pipeline

## Files touched
- `cai.py` — added `_pre_screen_issue_actionability()` function and inserted call in `cmd_fix` before the clone block

## Files read (not touched)
- `README.md` — lifecycle diagram and command table

## Key symbols
- `_pre_screen_issue_actionability(issue)` — new function in cai.py; calls Haiku via `_run_claude_p` to classify issue as actionable/spike/ambiguous
- `cmd_fix` — call inserted before clone block (around line 1460 in original)

## Design decisions
- Pre-screen biased toward `actionable` to minimize false positives (gate, not filter)
- On error, pre-screen defaults to `actionable` (fail-open)
- Model: `claude-haiku-4-5` (cheap, fast)

## Out of scope / known gaps
- `_run_plan_select_pipeline` not modified
- Label lifecycle for issues reaching fix agent unchanged
- No `--add-dir` flag on pre-screen call

## Invariants this change relies on
- Issue lock (`:in-progress`) held before pre-screen call
- `_set_labels` and `_run_claude_p` available at call site

---

## Revision 1 (2026-04-12)

### Rebase
- resolved: README.md (conflict in `cai.py fix` table row — main had spike row + original fix description; PR had updated fix description with pre-screen; merged both)

### Files touched this revision
- `README.md`:93-108 — updated lifecycle diagram to show pre-screen (Haiku) gate between `in-progress` and the three-way fork, added (spike)/(actionable) branch labels, updated rollback annotation to mention pre-screen ambiguous path

### Decisions this revision
- Used annotation on existing right-column return arrow (rather than a second visual column) to represent the ambiguous rollback — keeps diagram compact and readable
- Kept original fork column positions (`┌─────────────┼───────┐│`) to minimize diagram churn
- Updated `cai.py fix` table row to merge both main's scoring description and PR's pre-screen description

### New gaps / deferred
- None
