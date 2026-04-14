# PR Context Dossier
Refs: robotsix/robotsix-cai#620

## Files touched
- `.cai-staging/CLAUDE.md` → `.claude/CLAUDE.md` (new shared boilerplate file, applied via cai.py staging)
- `.cai-staging/agents/cai-implement.md` → removes lines 19-120 (working-dir + staging sections)
- `.cai-staging/agents/cai-revise.md` → removes `## Working directory` + `## Self-modifying agent files and plugins` sections
- `.cai-staging/agents/cai-fix-ci.md` → removes `## Working directory` + `## Self-modifying agent files` sections
- `.cai-staging/agents/cai-plan.md` → removes `## Your working directory...` section; preserves "The plan you produce..." paragraph
- `.cai-staging/agents/cai-rebase.md` → removes `## Your working directory...` section
- `.cai-staging/agents/cai-review-pr.md` → removes `## Your working directory...` section
- `.cai-staging/agents/cai-review-docs.md` → removes `## Your working directory...` section
- `.cai-staging/agents/cai-explore.md` → removes `## Your working directory...` section
- `.cai-staging/agents/cai-spike.md` → removes `## Your working directory...` section
- `.cai-staging/agents/cai-propose.md` → removes `## Your working directory` section
- `.cai-staging/agents/cai-update-check.md` → removes `## Your working directory...` section
- `.cai-staging/agents/cai-code-audit.md` → removes `## Your working directory...` section
- `.cai-staging/agents/cai-select.md` → removes `## Your working directory` section
- `.cai-staging/agents/cai-propose-review.md` → removes `## Your working directory` section
- `cai.py:1824` — added `CLAUDE_MD_STAGING_REL` constant
- `cai.py:1922` — added CLAUDE.md apply block in `_apply_agent_edit_staging()`

## Files read (not touched) that matter
- `.claude/agents/cai-implement.md` — primary source for CLAUDE.md content (lines 19-120)
- `cai.py:1820-1936` — staging mechanism to understand how to add CLAUDE.md support

## Key symbols
- `CLAUDE_MD_STAGING_REL` (`cai.py:1826`) — new constant for `.cai-staging/CLAUDE.md` path
- `_apply_agent_edit_staging()` (`cai.py:1841`) — extended to handle CLAUDE.md staging
- `_setup_agent_edit_staging()` (`cai.py:1827`) — unchanged; `.cai-staging/` parent dir is sufficient

## Design decisions
- Added `cai.py` CLAUDE.md staging support — plan assumed `.claude/CLAUDE.md` was unprotected, but it's also a sensitive path; staging via `.cai-staging/CLAUDE.md` was the only option
- No "See CLAUDE.md" replacement hints in agent files — admin confirmed auto-injection makes pointers redundant
- Preserved "The plan you produce will be consumed by the fix agent…" paragraph in `cai-plan.md` — plan-specific context, not generic boilerplate

## Out of scope / known gaps
- `_work_directory_block()` in `cai.py` still injects working-dir boilerplate via user message — not removed (separate concern from agent definition files)
- 9 read-only agents (`cai-refine`, `cai-analyze`, `cai-audit`, `cai-audit-triage`, `cai-merge`, `cai-unblock`, `cai-check-workflows`, `cai-confirm`, `cai-git`) not touched — they don't have the boilerplate

## Invariants this change relies on
- Claude Code auto-injects `.claude/CLAUDE.md` into every `claude -p` session launched from `cwd=/app`
- All `_run_claude_p()` calls in `cai.py` inherit `cwd=/app` (no explicit `cwd` override)
- `.cai-staging/` parent directory is pre-created by `_setup_agent_edit_staging()` before `_apply_agent_edit_staging()` runs
