# PR Context Dossier
Refs: robotsix/robotsix-cai#509

## Files touched
- `cai.py:234` — `_write_active_job` signature changed from `(cmd, issue: int)` to `(cmd, target_type, target_id: int | None)`; JSON fields `"target_type"` and `"target_id"` replace bare `"issue"`
- `cai.py:2260` — `cmd_fix` call site updated to `_write_active_job("fix", "issue", issue_number)`
- `cai.py:3219` — `cmd_revise` call site updated to `_write_active_job("revise", "issue", issue_number)`
- `cai.py:7433` — `cmd_spike` call site updated to `_write_active_job("spike", "issue", issue_number)`
- `cai.py:7700` — `cmd_explore` call site updated to `_write_active_job("explore", "issue", issue_number)`
- `cai.py:990` — `cmd_analyze`: added write/try-finally/clear around `_run_claude_p` call
- `cai.py:4339` — `cmd_audit`: added write/try-finally/clear around `_run_claude_p` call
- `cai.py:5110` — `cmd_cost_optimize`: added write/try-finally/clear around `_run_claude_p` call only
- `cai.py:5303` — `cmd_propose`: added write/try-finally/clear around creative `_run_claude_p` call only
- `cai.py:5685` — `cmd_update_check`: added write/try-finally/clear around `_run_claude_p` call only
- `cai.py:5896` — `cmd_confirm`: added write/try-finally/clear around `_run_claude_p` call only
- `cai.py:6229` — `cmd_review_pr`: added `_write_active_job("review-pr", "pr", pr_number)` before agent call; `_clear_active_job()` in existing `finally` block
- `cai.py:6452` — `cmd_review_docs`: same pattern as `cmd_review_pr`
- `cai.py:7002` — `cmd_merge`: added write before agent call, inline clear after it (no try/finally — matches existing error-handling style in that loop)
- `cai.py:7229` — `cmd_refine`: added write/try-finally/clear around `_run_claude_p` call

## Files read (not touched) that matter
- `cai.py` (lines 234–254) — existing `_write_active_job`/`_clear_active_job` definitions

## Key symbols
- `_write_active_job` (`cai.py:234`) — now takes `target_type: str` and `target_id: int | None` instead of bare `issue: int`
- `_clear_active_job` (`cai.py:249`) — unchanged; called in finally blocks for all new sites

## Design decisions
- `cmd_confirm` uses `target_type="none"` with a single write/clear pair — because it batches all issues in one agent call, not a per-issue loop
- `cmd_propose`, `cmd_update_check`, `cmd_cost_optimize`: write/clear wraps only the primary agent call (not the full function) since the subsequent logic is fast
- `cmd_merge`: bare `_clear_active_job()` after agent call rather than try/finally — consistent with existing error-handling style in that loop (no try/except wrapping the agent call)
- `cmd_review_pr`/`cmd_review_docs`: `_clear_active_job()` added to existing `finally` blocks so it fires on both success and exception paths
- Rejected: wrapping all of `cmd_cost_optimize` in try/finally — would require re-indenting ~115 lines of complex code

## Out of scope / known gaps
- `cmd_confirm` does not write per-issue as described in issue plan item 9 — the actual code batches all issues in one agent call, so per-issue tracking would require architectural changes
- `cmd_merge` has no try/finally around the agent call, so if `_run_claude_p` raises unexpectedly, `_clear_active_job` won't fire — matches existing error-handling philosophy

## Invariants this change relies on
- `_write_active_job` and `_clear_active_job` both never raise (OSError is caught)
- `_run_claude_p` does not raise exceptions in normal operation (it returns a CompletedProcess-like object with returncode)

## Revision 1 (2026-04-13)

### Rebase
- clean

### Files touched this revision
- `cai.py:4563` — added `_write_active_job("audit-triage", "none", None)` + try/finally around `_run_claude_p` in `cmd_audit_triage`
- `cai.py:5551` — added `_write_active_job("code-audit", "none", None)` + try/finally around `_run_claude_p` in `cmd_code_audit`
- `cai.py:8576` — added `_write_active_job("check-workflows", "none", None)` + try/finally around `_run_claude_p` in `cmd_check_workflows`
- `docs/configuration.md:37` — updated cai-active.json description to reflect new JSON schema and broader command coverage

### Decisions this revision
- Wrapped only the `_run_claude_p` call (not full function) in try/finally — matches `cmd_cost_optimize` pattern; subsequent logic (verdict parsing, publish) is fast enough not to warrant full wrapping
- Used `target_type="none"` for all three commands — they don't target a specific issue or PR

### New gaps / deferred
- (none)

## Revision 2 (2026-04-13)

### Rebase
- clean

### Files touched this revision
- `docs/configuration.md:70` — changed `(issue/PR/none)` to `(issue/pr/none)` to match actual string values used in `_write_active_job()` calls

### Decisions this revision
- Lowercase "pr" matches the actual `target_type` string values in the code; uppercase "PR" was a documentation typo

### New gaps / deferred
- (none)
