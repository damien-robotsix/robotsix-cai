# PR Context Dossier
Refs: robotsix/robotsix-cai#533

## Files touched
- `cai_lib/github.py`:3 — added `import re`
- `cai_lib/github.py`:140-168 — added `_fetch_linked_issue_block(pr_body: str) -> str` helper
- `cai.py`:194-197 — added `_fetch_linked_issue_block` to import
- `cai.py`:5828,5842 — added `body` to `--json` fields in `cmd_review_pr()` both branches
- `cai.py`:5926 — compute `issue_block` before user_message construction in `cmd_review_pr()`
- `cai.py`:5934 — inject `issue_block` before `## PR diff` in `cmd_review_pr()` user message
- `cai.py`:6079,6093 — added `body` to `--json` fields in `cmd_review_docs()` both branches
- `cai.py`:6205 — compute `issue_block` before user_message construction in `cmd_review_docs()`
- `cai.py`:6213 — inject `issue_block` before `## PR diff` in `cmd_review_docs()` user message
- `.cai-staging/agents/cai-review-pr.md` — added item 3 (Original issue) to What you receive, added `issue_drift` category row, added step 2 in How to work, renumbered steps
- `.cai-staging/agents/cai-review-docs.md` — added item 3 (Original issue) to What you receive, added step 2 in How to work, renumbered steps

## Files read (not touched) that matter
- `cai_lib/github.py` — confirmed `REPO` already imported; confirmed `subprocess`/`json` available
- `cai.py` (offset 5820-5950) — exact structure of `cmd_review_pr()` user message construction
- `cai.py` (offset 6068-6220) — exact structure of `cmd_review_docs()` user message construction

## Key symbols
- `_fetch_linked_issue_block` (`cai_lib/github.py`:140) — parses `Refs REPO#N` from PR body, fetches issue, returns formatted block or ""
- `cmd_review_pr` (`cai.py`:5816) — PR review orchestration; now injects issue block into agent user message
- `cmd_review_docs` (`cai.py`:6068) — docs review orchestration; now injects issue block into agent user message

## Design decisions
- Used `cai_lib/github.py` for helper — consistent with existing `_build_issue_block` / `_build_fix_user_message` placement
- Parsed `Refs REPO#N` from PR body rather than branch name — more robust; canonical linking convention used throughout codebase
- Graceful fallback (`try/except`, return `""`) — review proceeds normally when issue is missing/deleted/inaccessible
- `body` is fetched but only the parsed issue number is forwarded to the agent (PR body itself is discarded)

## Out of scope / known gaps
- Did not change `cai-revise` agent — it already has the PR body context from its own flow
- `issue_drift` findings by `cai-review-pr` will require the revise agent to address them like any other finding

## Invariants this change relies on
- Auto-improve PRs always embed `Refs robotsix/robotsix-cai#N` in their body (set by wrapper at PR creation time)
- `_gh_json` raises `subprocess.CalledProcessError` on failure — the try/except handles this correctly
