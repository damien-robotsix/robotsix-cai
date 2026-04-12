# PR Context Dossier
Refs: robotsix-cai/robotsix-cai#446

## Files touched
- cai.py:3077 — added early-exit block (3c) for clean-rebase + no-comments case
- cai.py:3237 — added `rebase_only`/`agent_name` conditional before step 6
- cai.py:3260 — updated log/agent invocation to use `agent_name` instead of hardcoded `cai-revise`
- cai.py:1890 — added `cai-rebase` to the cloned-worktree subagents comment
- .cai-staging/agents/cai-rebase.md — new haiku agent definition for rebase-only conflict resolution

## Files read (not touched) that matter
- .claude/agents/cai-revise.md — source for working-directory rules, git delegation pattern, and rebase loop steps reproduced in cai-rebase.md

## Key symbols
- `cmd_revise` (cai.py:~2940) — the function containing all three changes
- `rebase_in_progress` (cai.py:3032) — boolean set after rebase attempt; used by early-exit and routing
- `comments` (cai.py:2964) — unaddressed review comments from `_select_revise_targets()`; used by early-exit and routing
- `pre_agent_head` (cai.py:3023) — HEAD before rebase, used by early-exit to detect if push is needed
- `_run_claude_p` (cai.py:~3263) — agent invocation helper; now receives `agent_name` instead of hardcoded string

## Design decisions
- Early exit checks `not rebase_in_progress and not comments` (clean rebase + zero comments = no agent needed)
- Early exit uses `--force-with-lease` (same as step 10) and only pushes when HEAD actually moved
- `rebase_only = rebase_in_progress and not comments` routes to haiku only when rebase conflicts exist but no review comments; if both exist, full sonnet cai-revise handles both in one session
- cai-rebase has no memory tracking — mechanical conflict resolution doesn't need pattern tracking
- Staging setup (`_setup_agent_edit_staging`) left unconditional for both agents — harmless for cai-rebase

## Out of scope / known gaps
- cai-rebase does not write a PR context dossier (no review comments to record)
- Post-agent verification (step 7) is unchanged — works identically for both agents

## Revision 1 (2026-04-12)

### Rebase
- clean

### Files touched this revision
- cai.py:181 — added `cai-rebase` to module-level cloned-worktree agent list comment
- README.md:486 — added rebase, update-check, plan, select, git to cloned-worktree agents list
- docker-compose.yml:81 — updated cloned-worktree agents list to include all agents; corrected stale "copied in/out" description

### Decisions this revision
- Used complete agent list (all 11) in all three locations to match the authoritative list at cai.py:1890

### New gaps / deferred
- None

## Revision 2 (2026-04-12)

### Rebase
- clean

### Files touched this revision
- Dockerfile:84-92 — updated cloned-worktree memory comment from "copied in/out by wrapper" to "direct volume access; cai-rebase excluded"
- docker-compose.yml:76-86 — dropped `rebase` from the memory-tracking agent list; added note that cai-rebase is excluded
- README.md:481-492 — dropped `rebase` from the memory-tracking agent list; added note that cai-rebase is excluded

### Decisions this revision
- Qualified all three documentation sites to note cai-rebase is an exception (no `memory: project`, no memory tracking) rather than adding memory tracking to cai-rebase — the design decision to keep it lightweight is intentional
- Dockerfile comment updated to match docker-compose.yml/README.md wording established in Revision 1

### New gaps / deferred
- None

## Revision 3 (2026-04-12)

### Rebase
- clean

### Files touched this revision
- README.md:176-178 — updated revise workflow description to reflect new routing: clean+no-comments auto-push, conflicts+no-comments → cai-rebase, any-rebase+comments → cai-revise; kept human-triage fallback for ambiguous conflicts

### Decisions this revision
- Expanded description to three cases (no-op, cai-rebase, cai-revise) as suggested by reviewer

### New gaps / deferred
- None

## Invariants this change relies on
- `comments` at line 2964 contains only unaddressed comments (filtered by `_select_revise_targets`)
- `rebase_in_progress` is accurate — set immediately after the rebase attempt with no intervening git ops

## Revision 6 (2026-04-12)

### Rebase
- clean

### Files touched this revision
- cai.py:3028-3033 — updated comment from "unified cai-revise subagent handles both" to three-case routing description matching actual logic
- .cai-staging/agents/cai-revise.md — updated frontmatter description and opening paragraph to clarify cai-revise is only invoked when there are unaddressed review comments; added explicit note that conflict-only runs go to cai-rebase

### Decisions this revision
- Kept the safety-net paragraph ("If the rebase was already clean … print a short confirmation sentence and exit") in cai-revise.md — it handles edge cases where all comments are filtered as already-addressed after context analysis

### New gaps / deferred
- None

## Revision 4 (2026-04-12)

### Rebase
- clean

### Files touched this revision
- .claude/agents/cai-git.md:12 — updated "primarily `cai-revise`" to "primarily `cai-revise` and `cai-rebase`" to reflect new caller

### Decisions this revision
- Kept "primarily" qualifier since other agents (cai-revise, cai-rebase) are the main callers but the contract is open to any subagent

### New gaps / deferred
- None

## Revision 5 (2026-04-12)

### Rebase
- clean

### Files touched this revision
- .claude/agents/cai-rebase.md — added "PR context dossier — why you skip it" section explaining that cai-rebase explicitly ignores the wrapper's no-dossier instruction to create a minimal dossier; documents the exception with rationale

### Decisions this revision
- Resolved contradictory_rules finding: cai-rebase.md previously said "no PR dossier to write" without acknowledging the wrapper's instruction; new section makes the exception explicit and explains why (mechanical conflict-only edits, no design decisions, next cai-revise will create dossier if comments arrive)

### New gaps / deferred
- None
