# PR Context Dossier
Refs: robotsix/robotsix-cai#575

## Files touched
- cai.py:2481-2508 — Added `_REVIEW_PR_FOOTER_SENTINEL`, `_REVIEW_PR_PREAMBLE` constants and `_strip_review_pr_boilerplate()` helper
- cai.py:3353-3361 — Applied stripping in the `comments_section` loop for review-pr findings comments

## Files read (not touched) that matter
- cai.py:6022-6043 — Shows exact footer text added by `cmd_review_pr` to findings comments
- cai.py:2463-2472 — `_BOT_COMMENT_MARKERS` explains why clean comments are already filtered; findings comments flow through

## Key symbols
- `_strip_review_pr_boilerplate` (cai.py:2491) — strips footer (via `\n---\n` sentinel) and preamble from review-pr comment bodies
- `_REVIEW_COMMENT_HEADING_FINDINGS` (cai.py:5859) — used to detect review-pr findings comments in the loop
- `_REVIEW_COMMENT_HEADING_CLEAN` (cai.py:5860) — guard: clean comments are already filtered by `_BOT_COMMENT_MARKERS` but double-checked

## Design decisions
- Sentinel-string detection, not author login — the bot uses the human operator's gh token, so login-based filtering is unreliable (see comment at line 2450-2453)
- `rfind("\n---\n")` for footer — matches the last separator to avoid false positives if finding bodies contain `---` themselves
- `replace()` for preamble — the preamble appears once at most; `replace` + `strip` is simpler than index-based removal

## Out of scope / known gaps
- Other agent preambles (not "Now I have enough information...") are not stripped; only the specific string from issue #575 is targeted
- Docs review comments (`## cai docs review`) are not stripped; they're already filtered by `_BOT_COMMENT_MARKERS` (clean/applied variants) or don't have this boilerplate

## Invariants this change relies on
- Clean review-pr comments (`## cai pre-merge review (clean)`) are always in `_BOT_COMMENT_MARKERS` and filtered before reaching `comments_section`
- The footer always follows the `\n---\n` pattern as constructed in `cmd_review_pr` at line ~6029-6032
