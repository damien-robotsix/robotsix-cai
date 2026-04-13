# PR Context Dossier
Refs: robotsix/robotsix-cai#539

## Files touched
- `cai.py:1420-1597` — removed `AGENT_EDIT_STAGING_REL`, `PLUGIN_STAGING_REL` constants, `_setup_agent_edit_staging`, and `_apply_agent_edit_staging` functions
- `cai.py:1425-1452` — simplified `_work_directory_block` docstring (removed staging references)
- `cai.py:1453-1454` — removed `staging_rel`/`staging_abs` local variables
- `cai.py:1481-1513` — removed "## Updating `.claude/agents/*.md`" staging section from returned string
- `cai.py:1705-1714` — removed `_setup_agent_edit_staging(work_dir)` call and comment block in cai-fix pipeline
- `cai.py:1719-1721` — updated comment to remove staging reference
- `cai.py:1749-1754` — updated `--dangerously-skip-permissions` comment to remove staging reference
- `cai.py:1784-1800` — removed `_apply_agent_edit_staging` call and comment block in cai-fix pipeline
- `cai.py:2930-2935` — removed `_setup_agent_edit_staging(work_dir)` call and comment in cai-revise pipeline
- `cai.py:2975-2991` — removed `_apply_agent_edit_staging` call and comment in cai-revise pipeline
- `.claude/agents/cai-fix.md` — removed "## Self-modifying `.claude/agents/*.md` and `.claude/plugins/` (staging directory)" section
- `.claude/agents/cai-revise.md` — removed "## Self-modifying `.claude/agents/*.md` and `.claude/plugins/` (staging directory)" section

## Files read (not touched) that matter
- `cai.py` — primary source; staging infrastructure was concentrated in lines 1420-1597

## Key symbols
- `_setup_agent_edit_staging` (`cai.py`, removed) — created `.cai-staging/agents/` and `.cai-staging/plugins/` dirs before agent invocation
- `_apply_agent_edit_staging` (`cai.py`, removed) — copied staged files back to `.claude/agents/` and `.claude/plugins/` after agent exit
- `_work_directory_block` (`cai.py:1425`) — still exists; simplified to remove staging section from returned string

## Design decisions
- Removed all staging infrastructure rather than keeping it gated behind a flag — `git revert` provides rollback if the assumption proves wrong
- `shutil` import kept — still used extensively throughout `cai.py` for `rmtree`

## Out of scope / known gaps
- The core assumption (that `--dangerously-skip-permissions` now allows direct writes to `.claude/agents/*.md` in headless `-p` mode) is **untested** — this PR needs manual verification before merge
- Other agent definition files (cai-plan, cai-review-pr, etc.) receive `_work_directory_block` text but have no baked-in staging instructions — no changes needed there

## Invariants this change relies on
- `--dangerously-skip-permissions` now permits Edit/Write on `.claude/agents/*.md` paths in headless `claude -p` sessions (the whole premise of this change)
- `shutil` is still imported and used elsewhere — removing it is not part of this change
