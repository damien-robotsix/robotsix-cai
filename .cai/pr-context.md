# PR Context Dossier
Refs: robotsix-cai/robotsix-cai#522

## Files touched
- `cai.py:6419` — added gate in `cmd_review_docs` to skip PRs whose HEAD SHA has not yet been reviewed by `cai review-pr`

## Files read (not touched) that matter
- `cai.py:6827–6847` — `cmd_merge` filter 7 uses the identical `startswith(_REVIEW_COMMENT_HEADING_FINDINGS) and head_sha in first_line` pattern; this fix mirrors it exactly

## Key symbols
- `_REVIEW_COMMENT_HEADING_FINDINGS` (`cai.py:6115`) — prefix `"## cai pre-merge review"`, matches both findings and clean variants (clean starts with same prefix)
- `cmd_review_docs` (`cai.py:6355`) — function where the gate was added
- `skipped` counter — incremented for both the already-reviewed skip and the new gate skip so summary log stays accurate

## Design decisions
- Reuse existing `startswith(_REVIEW_COMMENT_HEADING_FINDINGS) and head_sha in first_line` pattern — mirrors `cmd_merge` filter 7 exactly, no new logic
- Gate applies even for `--pr N` direct targeting — consistent with review-pr's own behavior; explicit bypass not provided
- Rejected: adding a new label to track review-pr completion — more moving parts for no correctness benefit over comment-SHA check

## Out of scope / known gaps
- No label state machine changes — the gate is purely comment-presence based
- No bypass flag added — if needed in future, would be a separate issue

## Invariants this change relies on
- `cai review-pr` always posts a comment whose first line starts with `_REVIEW_COMMENT_HEADING_FINDINGS` and contains the head SHA
- PR comments are fetched with sufficient depth in `cmd_review_docs` to include review-pr comments

## Revision 1 (2026-04-13)

### Rebase
- clean

### Files touched this revision
- `docs/cli.md:115` — added Note block explaining review-pr → review-docs ordering enforcement
- `docs/architecture.md:10` — expanded Review step description to document ordering constraint and enforcement mechanism

### Decisions this revision
- Verbatim wording from reviewer suggestion used for both doc sites — accurate and consistent

### New gaps / deferred
- None
