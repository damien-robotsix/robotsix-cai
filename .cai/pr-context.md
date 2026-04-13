# PR Context Dossier
Refs: robotsix-cai/robotsix-cai#566

## Files touched
- `cai.py:3788` — added `_pr_set_pipeline_state(pr_number, LABEL_PR_EDITED)` in `cmd_revise` after push success
- `cai.py:6336-6338` — added `_pr_set_pipeline_state` calls (reject/accept) in `cmd_review_pr` after posting review comment
- `cai.py:6462-6470` — replaced SHA-scan gate in `cmd_review_docs` with `pr_labels` label check for `LABEL_PR_REVIEWED_ACCEPT`
- `cai.py:6602-6609` — added `_pr_set_pipeline_state` calls (edited/documented) in `cmd_review_docs` after posting comment
- `cai.py:6896` — extracted `pr_labels` to top of per-PR loop in `cmd_merge`
- `cai.py:6946-6955` — replaced entire SHA-scan filter 7 block in `cmd_merge` with label check (reviewed-accept OR documented)
- `cai.py:6958-6963` — replaced SHA-scan filter 7b in `cmd_merge` with label check (documented)
- `cai.py:6392-6396` — removed `_REVIEW_DOCS_COMMIT_SUBJECT` constant definition (5 lines)
- `cai.py:6564-6566` — inlined the constant string into the `_git commit -m` call in `cmd_review_docs`

## Files read (not touched) that matter
- `cai.py` — `_pr_set_pipeline_state` function (line 6688), label constants (lines 6680-6683), `_DOCS_REVIEW_COMMENT_HEADING_PREFIX` (line 6390, still live in idempotency check at ~6434)

## Key symbols
- `_pr_set_pipeline_state` (`cai.py:6688`) — removes all existing pr:* labels and sets the new one atomically
- `LABEL_PR_EDITED` (`cai.py:6680`) — "pr:edited", set after push in cmd_revise and docs-fix push in cmd_review_docs
- `LABEL_PR_REVIEWED_ACCEPT` (`cai.py:6682`) — "pr:reviewed-accept", gate for cmd_review_docs and filter 7 in cmd_merge
- `LABEL_PR_REVIEWED_REJECT` (`cai.py:6681`) — "pr:reviewed-reject", set when review-pr finds issues
- `LABEL_PR_DOCUMENTED` (`cai.py:6683`) — "pr:documented", gate for filter 7b in cmd_merge, set by cmd_review_docs clean
- `pr_labels` (`cai.py:6896`) — set computed once per loop iteration in cmd_merge, shared by filters 7 and 7b

## Design decisions
- `pr_labels` computed at top of cmd_merge loop (before filter 1) — harmless for non-bot PRs since they continue immediately; avoids re-scoping inside filter 7
- Label set in cmd_revise immediately after push (before comment) — reflects branch state change, not comment posting
- `_DOCS_REVIEW_COMMENT_HEADING_PREFIX` NOT removed — still used in cmd_review_docs idempotency check (~line 6434)
- Bootstrap: PRs with no pr:* label skip cmd_review_docs and cmd_merge until next cmd_review_pr run sets label

## Out of scope / known gaps
- `_pr_label_sweep` changes deferred to step 3 (#557)
- Safety filters 1-6 and 3 (unaddressed review comments) not touched
- `needs-human-review` and `merge-blocked` logic not touched

## Invariants this change relies on
- `_pr_set_pipeline_state` is idempotent — calling it multiple times with same label is safe
- Label constants (`LABEL_PR_*`) defined at module level (line 6674+); Python resolves in function bodies at call-time, so cmd_review_docs using them is fine despite being defined before the constants in the file
