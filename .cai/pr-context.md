# PR Context Dossier
Refs: robotsix-cai/cai#525

## Files touched
- `.claude/agents/cai-review-docs.md` (via staging) — added `Edit, Write` tools, rewrote instructions to fix docs directly instead of emitting findings, updated output format to `### Fixed: stale_docs`
- `cai.py:6366` — added `headRefName` to `gh pr view` JSON fields
- `cai.py:6380` — added `headRefName` to `gh pr list` JSON fields
- `cai.py:6400` — extracted `branch = pr.get("headRefName", "")` from PR data
- `cai.py:6459–6484` — replaced `git clone --depth 1` with `gh repo clone` + fetch/checkout branch + git identity config; added `gh auth setup-git`
- `cai.py:6523–6586` — after agent runs: detect file changes via `git status --porcelain`, commit + push if changes exist, post FINDINGS comment at new SHA; otherwise post clean/findings comment at original SHA

## Files read (not touched) that matter
- `cai.py` (lines 3200–3660) — `cmd_revise` pattern for `gh repo clone`, branch checkout, git identity, and `git push`
- `cai.py` (lines 6870–6891) — merge gate check for `_DOCS_REVIEW_COMMENT_HEADING_FINDINGS`; `startswith` already matches CLEAN heading so no merge gate change needed

## Key symbols
- `cmd_review_docs` (`cai.py:6355`) — main function modified
- `_DOCS_REVIEW_COMMENT_HEADING_FINDINGS` (`cai.py:6351`) — used for both "fixed" and unfixable findings comments
- `_git` (`cai.py:2047`) — used for status, add, commit, fetch, checkout, config, rev-parse
- `_gh_user_identity` (`cai.py:1046`) — resolves git name/email for commits

## Design decisions
- Used `gh repo clone` (full clone) instead of `--depth 1` to allow push back to origin — shallow clones can't push
- After fixing and pushing, post FINDINGS comment at the *new* SHA so: (a) idempotency check skips it on next run, (b) merge gate finds it and proceeds after review-pr reviews the new SHA
- Kept fallback path for "### Finding:" output (agent couldn't fix) to handle edge cases like missing docs directory
- `startswith(_DOCS_REVIEW_COMMENT_HEADING_FINDINGS)` already matches the CLEAN heading, so idempotency and merge gate work correctly for both cases without additional changes

## Out of scope / known gaps
- The merge gate (line 6878) does not check `_DOCS_REVIEW_COMMENT_HEADING_CLEAN` explicitly but it works because CLEAN starts with the FINDINGS prefix — no change needed
- After review-docs pushes a new commit (new SHA=Y), review-pr must re-run to review Y before merge proceeds — this is the correct pipeline ordering

## Revision 1 (2026-04-13)

### Rebase
- clean

### Files touched this revision
- `cai.py:6337-6361` — updated comment block for `_DOCS_REVIEW_COMMENT_HEADING_FINDINGS` to document both code paths (fixed+pushed vs unfixable findings)
- `cai.py:6356` — updated `cmd_review_docs` docstring from "post findings as PR comments" to "fix stale documentation and post findings for issues that cannot be fixed automatically"

### Decisions this revision
- docs/agents.md, docs/architecture.md, docs/cli.md were already fixed and pushed by cai-review-docs agent (second review comment); only cai.py internal comments/docstring remained

### New gaps / deferred
- none

## Invariants this change relies on
- `gh auth setup-git` must be called before `gh repo clone` to enable authenticated push
- The PR branch must not be protected against bot pushes (auto-improve branches are bot-owned)
- The agent writes doc changes to `work_dir` before exiting; the wrapper detects them via `git status --porcelain`
