# PR Context Dossier
Refs: robotsix-cai/robotsix-cai#620

## Files touched
- CLAUDE.md:1 — extended with working-directory and staging-directory sections (via staging: `.cai-staging/claudemd/CLAUDE.md`)
- .claude/agents/cai-implement.md — removed lines 19–138 (working directory + staging sections)
- .claude/agents/cai-plan.md — removed lines 17–35 (working directory section); preserved "The plan you produce will be consumed..." paragraph
- .claude/agents/cai-revise.md — removed lines 25–57 (Working directory + staging sections)
- .claude/agents/cai-fix-ci.md — removed lines 18–47 (Working directory + staging sections)
- .claude/agents/cai-rebase.md — removed lines 40–59 (working directory section)
- .claude/agents/cai-review-pr.md — removed lines 17–37 (working directory section)
- .claude/agents/cai-review-docs.md — removed lines 34–52 (working directory section)
- .claude/agents/cai-explore.md — removed lines 27–35 (working directory section)
- .claude/agents/cai-propose.md — removed lines 17–30 (working directory section)
- .claude/agents/cai-update-check.md — removed lines 18–35 (working directory section)
- .claude/agents/cai-code-audit.md — removed lines 18–41 (working directory section)
- .claude/agents/cai-select.md — removed lines 15–32 (working directory section)
- .claude/agents/cai-propose-review.md — removed lines 23–27 (working directory section)

## Files read (not touched) that matter
- cai_lib/cmd_helpers.py — confirmed staging mechanism: `.cai-staging/claudemd/CLAUDE.md` → `<work_dir>/CLAUDE.md`
- CLAUDE.md — existing root-level file with efficiency guidance; new content was merged in

## Key symbols
- `_apply_agent_edit_staging` (cai_lib/cmd_helpers.py:196) — wrapper function that copies staged files to their targets
- `CLAUDEMD_STAGING_REL` (cai_lib/cmd_helpers.py) — constant defining `.cai-staging/claudemd/` staging path

## Design decisions
- Merged into root `CLAUDE.md` rather than creating `.claude/CLAUDE.md` — write protection on paths containing `.claude/` blocks staging to `.cai-staging/claudemd/.claude/CLAUDE.md`; root CLAUDE.md is already confirmed to be injected into all subagent sessions
- Preserved existing efficiency guidance in root CLAUDE.md; added working-directory and staging sections below it
- Rejected: creating `.claude/CLAUDE.md` as specified by issue — staging path `.cai-staging/claudemd/.claude/CLAUDE.md` is blocked by write protection on `.claude/` directory; direct write to `.claude/CLAUDE.md` is also blocked
- cai-spike.md silently skipped — does not exist in the clone

## Out of scope / known gaps
- 9 read-only agents (cai-refine, cai-analyze, cai-audit, cai-audit-triage, cai-merge, cai-unblock, cai-check-workflows, cai-confirm, cai-git) not touched
- The CLAUDE.md is at repo root, not `.claude/CLAUDE.md` as the issue specified

## Invariants this change relies on
- Root `CLAUDE.md` at `/app/CLAUDE.md` is auto-injected into all `claude -p --agent X` sessions since agents run with `cwd=/app`
- The wrapper's `_apply_agent_edit_staging` correctly maps `.cai-staging/claudemd/CLAUDE.md` → `<work_dir>/CLAUDE.md`
- All 13 modified agent files are staged in `.cai-staging/agents/` for the wrapper to copy to `.claude/agents/`
