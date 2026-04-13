# PR Context Dossier
Refs: robotsix-cai/robotsix-cai#574

## Files touched
- cai.py:2480 — added `_REVISE_ISSUE_BODY_MAX_CHARS = 1500` constant
- cai.py:3379-3397 — truncate issue body before inlining into revise user message

## Files read (not touched) that matter
- cai.py (lines 2440–2490, 3320–3400) — revise section: constant placement and user_message construction

## Key symbols
- `_REVISE_ISSUE_BODY_MAX_CHARS` (cai.py:2480) — cap for issue body chars in revise user message
- `cmd_revise` (cai.py:3023) — function that builds the user message for cai-revise agent
- `_issue_body_raw` / `_issue_body` (cai.py:3379-3387) — local vars for truncated body

## Design decisions
- Truncate at char boundary (not token boundary) — simple, deterministic, no tokenizer dependency
- Append `… (truncated — see #N for full body)` so agent knows truncation happened and where to find more
- Constant placed in revise section near other revise-specific constants, not at top of file

## Out of scope / known gaps
- No change to the `cai-implement` or `cai-fix` pipelines — they inline full issue bodies but are not addressed by this issue

## Invariants this change relies on
- `issue_data["number"]` is always present (fetched from GitHub API before this code runs)
- `_REVISE_ISSUE_BODY_MAX_CHARS` is module-level, so it can be patched in tests if needed
