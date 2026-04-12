# PR Context Dossier
Refs: robotsix/robotsix-cai#461

## Files touched
- `.claude/agents/cai-review-pr.md`:4 — removed `Agent` from `tools:` frontmatter
- `.claude/agents/cai-review-pr.md`:15 — removed "and the `Agent` tool" from body description
- `.claude/agents/cai-review-pr.md`:65-68 — removed "use the Agent tool with `subagent_type: Explore`" from step 2
- `.claude/agents/cai-review-pr.md`:141-145 — deleted entire item 5 "Use Agent for broad exploration" from Efficiency guidance
- `cai.py`:5754 — updated comment to remove `/Agent` from tool allowlist description
- `cai.py`:5779-5781 — added `--max-budget-usd 0.50` to the `claude -p` invocation

## Files read (not touched) that matter
- `.claude/agents/cai-review-pr.md` — source of all agent changes (via staging)

## Key symbols
- `cmd_review_pr` (`cai.py`:5664) — function that invokes the cai-review-pr agent
- `_run_claude_p` (`cai.py`:5778) — call site where `--max-budget-usd` was added

## Design decisions
- Budget cap set to $0.50 (matching the plan's recommendation) — below the observed worst-case $0.78 but above the median, giving a meaningful cap without cutting off normal runs
- `Agent` tool removed entirely rather than rate-limited — nested sub-sessions are the root cause of high turn counts, not just usage frequency
- Items 1–4 of efficiency guidance retained — Grep/Glob/batch-Read patterns are still valid and encourage efficient direct tool use

## Out of scope / known gaps
- `cai-review-docs` has a similar structure but was deliberately not changed in this PR per scope guardrails
- Did not change review categories or SHA-idempotency logic

## Invariants this change relies on
- `--max-budget-usd` is a valid Claude CLI flag supported by the current claude-code version
- Removing `Agent` from tools list still leaves `Read`, `Grep`, `Glob` sufficient for ripple-effect checking
