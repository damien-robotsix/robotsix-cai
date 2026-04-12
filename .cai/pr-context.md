# PR Context Dossier
Refs: damien-robotsix/robotsix-cai#308

## Files touched
- cai.py:470-480 — added `plugin_dir`/`plugin_flags` injection in `_run_claude_p` to pass `--plugin-dir` when the plugin directory exists
- cai.py:1522-1617 — extended `_apply_agent_edit_staging` to also copy `.cai-staging/plugins/` → `.claude/plugins/` using `shutil.copytree(dirs_exist_ok=True)` before the existing cleanup
- cai.py:915-936 — removed `_fetch_closed_auto_improve_issues` call and `closed_block` variable from `cmd_analyze`; updated surrounding comment to reference the new skill
- .cai-staging/plugins/cai-skills/.claude-plugin/plugin.json — new plugin manifest (wrapper moves to .claude/plugins/)
- .cai-staging/plugins/cai-skills/skills/look-up-closed-finding/SKILL.md — new skill definition (wrapper moves to .claude/plugins/)
- .cai-staging/agents/cai-analyze.md — updated agent: added Bash to tools; removed item 3 (closed issues) from Input format; updated Filter item 3 to use skill:look-up-closed-finding on-demand

## Files read (not touched) that matter
- .claude/agents/cai-analyze.md — current agent definition, used as base for staged update
- cai.py (lines 460-490, 905-950, 1505-1590, 2820-2840) — staging mechanism, _run_claude_p, cmd_analyze, cmd_revise

## Key symbols
- `_run_claude_p` (cai.py:451) — central helper for all `claude -p` invocations; plugin-dir flag injected here
- `_apply_agent_edit_staging` (cai.py:1522) — now also handles plugin staging at `.cai-staging/plugins/`
- `_fetch_closed_auto_improve_issues` (cai.py:663) — function kept (not deleted), only call site in `cmd_analyze` removed
- `skill:look-up-closed-finding` (.claude/plugins/cai-skills/skills/look-up-closed-finding/SKILL.md) — new on-demand skill replacing bulk closed-issues injection

## Design decisions
- Plugin files written to `.cai-staging/plugins/` (not directly to `.claude/plugins/`) because all `.claude/` writes are blocked by Claude Code's sensitive-file protection in headless mode
- Extended `_apply_agent_edit_staging` rather than adding a separate function to keep staging cleanup logic in one place
- `--plugin-dir` uses a relative path `Path(".claude/plugins/cai-skills")` — valid because `_run_claude_p` is called with the repo root as cwd
- Rejected: writing plugin files directly to `.claude/plugins/` — blocked by headless mode sensitive-file protection

## Out of scope / known gaps
- `_fetch_closed_auto_improve_issues` function definition left intact (not deleted) per scope guardrails; still called at line 4040
- `_closed_issues_block` removed in Revision 3 (orphaned dead code — its only call site in `cmd_analyze` was removed by the initial commit)
- `--plugin-dir` is injected for ALL `claude -p` calls, not just cai-analyze — intentional (future skills may be useful to other agents)
- No changes to publish.py, fingerprinting, or label lifecycle (ruled out as skill candidates)

## Invariants this change relies on
- `_run_claude_p` callers use repo root as cwd (so relative plugin path resolves correctly)
- `shutil.copytree` with `dirs_exist_ok=True` is available (Python 3.8+)
- The wrapper's `_apply_agent_edit_staging` is called after the fix agent exits in both `cmd_fix` and `cmd_revise`

## Revision 1 (2026-04-12)

### Rebase
- clean

### Files touched this revision
- cai.py:1481-1538 — expanded comment block to document plugin staging pattern; added PLUGIN_STAGING_REL constant
- cai.py:1547-1620 — updated _apply_agent_edit_staging docstring; used PLUGIN_STAGING_REL constant; added early return on plugin staging failure (fail-fast, preserve staged content)
- .cai-staging/agents/cai-fix.md — updated "Self-modifying" section to document plugin staging at .cai-staging/plugins/
- .cai-staging/agents/cai-revise.md — updated "Self-modifying" section to document plugin staging at .cai-staging/plugins/

### Decisions this revision
- Fail-fast on plugin staging error (return early, skip cleanup) — preserves staged content so it isn't silently lost; caller can inspect and retry
- PLUGIN_STAGING_REL constant placed adjacent to AGENT_EDIT_STAGING_REL — consistent naming, single source of truth for both paths

### New gaps / deferred
- None — all three reviewer findings addressed

## Revision 2 (2026-04-12)

### Rebase
- clean

### Files touched this revision
- .cai-staging/agents/cai-analyze.md — removed `Bash` from tools list (line 4); agent uses `skill:look-up-closed-finding` via the Skill tool, not via Bash directly

### Decisions this revision
- Removed Bash rather than updating the description: the agent genuinely does not need shell access; `gh` runs inside the skill runtime, not the agent. "Read-only" description remains accurate.

### New gaps / deferred
- None — reviewer finding addressed

## Revision 3 (2026-04-12)

### Rebase
- resolved: cai.py — conflict between main's new "Multi-step issue helpers" section and PR commit updating the staging comment title; kept both (all helper functions + updated title)

### Files touched this revision
- cai.py:768-799 — deleted orphaned `_closed_issues_block()` function (only call site was removed in initial commit; dead code)
- .cai-staging/plugins/cai-skills/.claude-plugin/plugin.json — created plugin manifest (wrapper moves to .claude/plugins/)
- .cai-staging/plugins/cai-skills/skills/look-up-closed-finding/SKILL.md — created skill definition (wrapper moves to .claude/plugins/)

### Decisions this revision
- Deleted `_closed_issues_block` per reviewer request; scope guardrails said not to delete `_fetch_closed_auto_improve_issues` (still called elsewhere), but `_closed_issues_block` had no remaining callers
- Plugin files created in `.cai-staging/plugins/` (wrapper applies to `.claude/plugins/` after session); this is the correct flow for agent sessions
- Finding 3 (cross_cutting_ref) was already resolved — `.claude/agents/cai-analyze.md` in the clone has `skill:look-up-closed-finding` reference at line 135; no changes needed

### New gaps / deferred
- None — all three reviewer findings addressed

## Revision 4 (2026-04-12)

### Rebase
- resolved: .claude/agents/cai-revise.md — conflict between HEAD's condensed staging section and PR commit's expanded version covering plugin staging; kept incoming (PR) version

### Files touched this revision
- .cai-staging/plugins/cai-skills/.claude-plugin/plugin.json — created plugin manifest (wrapper moves to .claude/plugins/)
- .cai-staging/plugins/cai-skills/skills/look-up-closed-finding/SKILL.md — created skill definition (wrapper moves to .claude/plugins/)
- .cai-staging/agents/cai-revise.md — conflict-resolved version (kept incoming expanded plugin staging section)

### Decisions this revision
- Plugin files written to .cai-staging/plugins/ — same flow as Revision 3; wrapper copies to .claude/plugins/ and commits; fixes missing_co_change where Revision 3's staging writes never landed in the repo

### New gaps / deferred
- None — reviewer finding addressed

## Revision 5 (2026-04-12)

### Rebase
- clean

### Files touched this revision
- cai.py:1779-1792 — updated `_setup_agent_edit_staging` to also create `.cai-staging/plugins/` directory; updated docstring to reflect both staging dirs

### Decisions this revision
- Added `plugin_staging.mkdir(parents=True, exist_ok=True)` alongside the existing agents staging mkdir — matches documentation stating the wrapper creates both dirs; prevents Write-tool failures when agents try to create plugin files under `.cai-staging/plugins/`

### New gaps / deferred
- None — all reviewer findings addressed

## Revision 6 (2026-04-12)

### Rebase
- clean

### Files touched this revision
- .cai-staging/agents/cai-analyze.md — added `Skill` to tools list (line 4: `tools: Read, Grep, Glob, Skill`)

### Decisions this revision
- Added `Skill` tool (not `Bash`) — the agent invokes `skill:look-up-closed-finding` via the Skill tool; `gh` runs inside the skill runtime, not the agent directly

### New gaps / deferred
- None — reviewer finding addressed
