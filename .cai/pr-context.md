# PR Context Dossier
Refs: robotsix-cai/robotsix-cai#648

## Files touched
- `cai_lib/actions/review_pr.py`:289–298 — added `"--allowedTools", "Read,Grep,Glob"` to `_run_claude_p` invocation
- `cai_lib/actions/review_docs.py`:166–175 — added `"--allowedTools", "Read,Grep,Glob,Edit,Write"` to `_run_claude_p` invocation
- `.claude/agents/cai-review-pr.md` (via staging) — added Hard rule 9: no Bash
- `.claude/agents/cai-review-docs.md` (via staging) — removed Agent from frontmatter tools, removed "Use Agent for broad exploration" guidance, added Hard rule 6: no Bash

## Files read (not touched) that matter
- `cai_lib/actions/review_pr.py` — identified the `_run_claude_p` invocation site
- `cai_lib/actions/review_docs.py` — identified the `_run_claude_p` invocation site
- `.claude/agents/cai-review-pr.md` — verified existing Hard rules, read full content for staging rewrite
- `.claude/agents/cai-review-docs.md` — verified existing Hard rules and Agent usage, read full content for staging rewrite

## Key symbols
- `_run_claude_p` (`cai_lib/actions/review_pr.py`:289, `review_docs.py`:166) — subprocess wrapper; `--allowedTools` flag inserted here
- `--allowedTools` — Claude CLI flag that enforces tool restrictions at process level, overriding any frontmatter-only constraints

## Design decisions
- `cai-review-pr` gets `Read,Grep,Glob` only — matches its frontmatter and read-only contract
- `cai-review-docs` gets `Read,Grep,Glob,Edit,Write` (no Agent) — per human commenter's suggestion to remove Agent; the agent can use Read/Grep/Glob for exploration instead of spawning sub-agents
- `Agent` removed from `cai-review-docs` frontmatter tools and "Use Agent for broad exploration" guidance removed to stay consistent with the process-level restriction
- Rejected: keeping Agent for review-docs — commenter asked whether it was needed; since it's an optimization (not essential) and tightens the cost/security boundary, it was dropped

## Revision 1 (2026-04-15)

### Rebase
- clean

### Files touched this revision
- `cai.py`:1932 — added `"--allowedTools", "Read,Grep,Glob"` to cai-cost-optimize invocation
- `cai.py`:2121 — added `"--allowedTools", "Read,Grep,Glob"` to cai-propose invocation
- `cai.py`:2182 — added `"--allowedTools", "Read,Grep,Glob"` to cai-propose-review invocation
- `cai.py`:2366 — added `"--allowedTools", "Read,Grep,Glob"` to cai-code-audit invocation
- `cai.py`:2499 — added `"--allowedTools", "Read,Grep,Glob"` to cai-update-check invocation
- `cai.py`:3283 — added `"--allowedTools", "Read,Grep,Glob"` to cai-check-workflows invocation

### Decisions this revision
- All six agents declare `tools: Read, Grep, Glob` in frontmatter — `--allowedTools "Read,Grep,Glob"` is the correct restriction for all of them
- Pattern matches exactly what was done for cai-review-pr in the original PR

### New gaps / deferred
- None

## Out of scope / known gaps
- Prior fix #382 that was "already merged but didn't work" — the root cause was `--permission-mode acceptEdits` not enforcing frontmatter tool restrictions; this PR fixes it at the CLI level

## Invariants this change relies on
- `--allowedTools` CLI flag takes precedence over `--permission-mode acceptEdits` and frontmatter tool declarations in Claude Code
- `cai-review-docs` can fulfill its job (grep for renames, edit stale docs) using only Read/Grep/Glob/Edit/Write without needing to spawn sub-agents
