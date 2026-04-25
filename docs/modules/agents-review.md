# agents-review

Subagent definitions that review open PRs before merge —
ripple-effect review, documentation review, merge-readiness
assessment, and PR-comment filtering. Invoked by handlers in
`cai_lib/actions/` as a PR advances through the
REVIEWING_CODE → REVIEWING_DOCS → APPROVED pipeline.

## Key entry points
- [`.claude/agents/review/cai-review-pr.md`](../../.claude/agents/review/cai-review-pr.md)
  — sonnet ripple-effect reviewer. Walks changed files, searches
  the broader codebase for inconsistencies the PR introduced but
  did not update, and emits `### Finding:` blocks the wrapper
  posts as a PR comment. Read-only.
- [`.claude/agents/review/cai-review-docs.md`](../../.claude/agents/review/cai-review-docs.md)
  — haiku docs-drift reviewer. Also owns `docs/modules.yaml` and
  `docs/modules/<name>.md` — keeps the module index and
  narratives in sync whenever a PR adds, renames, or deletes
  tracked source files.
- [`.claude/agents/review/cai-merge.md`](../../.claude/agents/review/cai-merge.md)
  — inline opus merge-readiness verdict (confidence + action).
- [`.claude/agents/review/cai-comment-filter.md`](../../.claude/agents/review/cai-comment-filter.md)
  — inline haiku that classifies PR comments as resolved or
  unresolved. Replaces the commit-timestamp watermark inside the
  revise handler.

## Inter-module dependencies
- Invoked by **actions** — `handle_review_pr` (cai-review-pr),
  `handle_review_docs` (cai-review-docs), `handle_merge`
  (cai-merge), `handle_revise` (cai-comment-filter via
  `_filter_comments_with_haiku`).
- Consumes **docs** — root `CLAUDE.md`; `cai-review-docs` also
  owns and edits files under `docs/` and `docs/modules/`.
- Uses **audit** — `cai-review-docs` relies on the modules
  registry schema (`docs/modules.yaml`) and may trigger
  coverage-check drift.
- Uses **agents-config** — permission/hook settings.

## Operational notes
- **Cost tiers.** `cai-review-pr` and `cai-review-docs` run on
  sonnet with Read/Grep/Glob (and Edit/Write for the docs
  reviewer); `cai-merge` is opus but inline-only (minimal tokens
  per call); `cai-comment-filter` is haiku.
- **Docs-review is owner of `docs/modules*`.** Any PR that adds,
  renames, or deletes a tracked source file must update
  `docs/modules.yaml` plus the matching `docs/modules/<name>.md`.
  `cai-review-docs` enforces this pre-merge; `cai-review-pr` must
  NOT list `docs/**` as off-limits in its scope guardrails
  (it is always allowed).
- **FSM invariant.** `cai-merge` emits a structured verdict; a
  missing/malformed `Confidence:` line diverts the PR and keeps
  it in REVIEWING_CODE rather than merging.
- **CI implications.** Stale narratives are a common failure
  mode; `scripts/check-modules-coverage.py` is the backstop,
  while `cai-review-docs` is the proactive enforcer.
