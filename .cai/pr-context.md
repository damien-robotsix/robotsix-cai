# PR Context Dossier
Refs: robotsix/robotsix-cai#424

## Files touched
- `cai.py:3341` — added Step 1e in `cmd_audit`: fetches `:pr-open` issues and calls `_recover_stale_pr_open`
- `cai.py:3457` — added recovered issues to `deterministic_section` after the `flagged_merged` block
- `cai.py:3488` — added `pr_open_recovered` field to error-path `log_run` call
- `cai.py:3503` — added `pr_open_recovered` field to success-path `log_run` call

## Files read (not touched) that matter
- `cai.py:896` — `_recover_stale_pr_open` function: takes a list of issues with `labels` field, filters `:in-progress`, calls `_find_linked_pr`, transitions CLOSED PRs back to `:refined`

## Key symbols
- `_recover_stale_pr_open` (`cai.py:896`) — already used in `cmd_fix` and `cmd_verify`; now also called deterministically in `cmd_audit`
- `LABEL_PR_OPEN` (`cai.py:168`) — `"auto-improve:pr-open"` label used to query the issue list
- `cmd_audit` (`cai.py:3320`) — the periodic audit tick that runs deterministic cleanup steps before invoking the Claude audit subagent

## Design decisions
- Placed as Step 1e (before Step 2 that gathers GitHub state for Claude) so recovery runs deterministically before the LLM audit sees the queue state
- Used `--json` fields `number,title,body,labels,createdAt,comments` — `labels` is required by `_recover_stale_pr_open` to determine which raised label to apply
- Kept the try/except pattern matching the existing Step 2a pattern so GitHub API failures degrade gracefully to an empty list

## Out of scope / known gaps
- Issue #385 label fix (the manual remediation) — that's an operational action, not a code change; the new audit step will auto-recover it on the next audit tick
- `_find_linked_pr` relies on GitHub search indexing; this is a known fragility shared with `cmd_verify` and is not changed here

## Invariants this change relies on
- `_recover_stale_pr_open` is idempotent: if verify already rolled back an issue, audit will not see it with `LABEL_PR_OPEN` and will skip it
- `_recover_stale_pr_open` already skips issues with `LABEL_IN_PROGRESS` (line 905), so active fix runs are not disrupted
