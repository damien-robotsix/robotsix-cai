# PR Context Dossier
Refs: damien-robotsix/robotsix-cai#532

## Files touched
- cai.py:2502 — added `"## CI-fix subagent:"` to `_BOT_COMMENT_MARKERS`
- cai.py:2578 — added `_CI_FIX_ATTEMPT_MARKER` constant after `_REBASE_FAILED_MARKER`
- cai.py:2876 — added `_select_ci_fix_targets()` function
- cai.py:3009 — added `_fetch_ci_failure_log()` helper
- cai.py:8089 — added `cmd_fix_ci()` function
- cai.py:8372 — updated `_drain_pending_prs` to include `fix-ci` step
- cai.py:9327 — added `fix-ci` argparse subcommand with `--pr` argument
- cai.py:9401 — added `"fix-ci": cmd_fix_ci` to handlers dict
- entrypoint.sh:54 — added `CAI_FIX_CI_SCHEDULE` env var (default `50 * * * *`)
- entrypoint.sh:73 — added crontab entry for `cai.py fix-ci`
- docs/architecture.md:12 — added step 7.5 CI Fix, updated step 4 drain description
- .cai-staging/agents/cai-fix-ci.md — new subagent definition (copied to .claude/agents/ by wrapper)

## Files read (not touched) that matter
- cai.py (cmd_revise, _select_revise_targets) — primary pattern reference for the new command
- .claude/agents/cai-revise.md — agent definition pattern reference

## Key symbols
- `_CI_FIX_ATTEMPT_MARKER` (cai.py:2583) — per-SHA loop guard marker constant
- `_select_ci_fix_targets` (cai.py:2876) — queue selection: open PRs with failing CI, no unaddressed comments, no prior fix attempt on current SHA
- `_fetch_ci_failure_log` (cai.py:3009) — fetches last 200 lines of `gh run view --log-failed` output
- `cmd_fix_ci` (cai.py:8089) — main command: clones, rebases, invokes cai-fix-ci, commits, pushes, posts marker
- `_drain_pending_prs` (cai.py:8372) — updated to run fix-ci between revise and review-pr

## Design decisions
- Reuses `LABEL_REVISING` as the lock label — same as revise, avoids new label, race prevented by skipping if :revising in `_select_ci_fix_targets`
- Always posts `_CI_FIX_ATTEMPT_MARKER` comment whether fix succeeded or not — guarantees loop guard fires
- Skips PRs with unaddressed review comments — leaves them for `cai revise`
- Aborts and skips on rebase conflicts — leaves conflict resolution for `cai revise`/`cai-rebase`
- Runs after revise and before review-pr in `_drain_pending_prs` — correct ordering: revise first (may clear unaddressed comments), then fix-ci, then review
- Default cron schedule `50 * * * *` — runs after revise at :30 and merge/cycle at :00
- Limits CI log to last 200 lines and at most 2 failing checks — token budget
- Rejected: modifying `cmd_revise` or `_select_revise_targets` — scope guardrail

## Out of scope / known gaps
- No flake detection — if failure is infrastructure/flaky, agent should output "cannot fix" and bail
- No auto-rerun of failed jobs — investigating the log is the whole point
- No new GitHub labels — the per-SHA marker comment is sufficient state

## Invariants this change relies on
- `_drain_pending_prs` runs revise before fix-ci, so revise has already released the lock by the time fix-ci checks for :revising
- `_BOT_COMMENT_MARKERS` prefix `"## CI-fix subagent:"` matches both `_CI_FIX_ATTEMPT_MARKER` (`"## CI-fix subagent: fix attempt"`) and any "cannot fix" message — ensures these comments don't appear as unaddressed review comments
- `gh pr list --json comments` returns issue-level comments (not line comments); `_fetch_review_comments` fetches line comments separately — both are needed for the unaddressed-comment check
