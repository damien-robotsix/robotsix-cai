# PR Context Dossier
Refs: damien-robotsix/robotsix-cai#479

## Files touched
- `cai.py:201` — removed `LABEL_EXPLORATION_DONE` constant
- `cai.py:7529-7547` — removed `_find_exploration_report_comment()` and `_has_human_comment_after()` helpers
- `cai.py:7550-7912` — rewrote `cmd_explore()`: removed two-phase design, replaced with single-phase spike-like pattern with `## Exploration Findings`/`## Refined Issue`/`## Exploration Blocked` outcomes
- `cai.py:7790` — added `_write_active_job("explore", issue_number)` after lock succeeds
- `cai.py:finally` — added `_clear_active_job()` in explore's finally block
- `cai.py:~7907` — added `has_exploration` check in cycle loop alongside `has_spike`
- `cai.py:~7923` — updated `has_raised` guard to also exclude `has_exploration`
- `cai.py:~7959` — added `if not has_fix_target and has_exploration: _run_step("explore", ...)` block after spike block
- `publish.py:87` — removed `("auto-improve:exploration-done", ...)` label tuple
- `.cai-staging/agents/cai-explore.md` — updated agent output format to use new outcome markers

## Files read (not touched) that matter
- `cai.py` (cmd_spike section, lines 7268-7522) — used as the model for the new cmd_explore pattern
- `.claude/agents/cai-explore.md` — read to understand existing agent instructions before rewriting

## Key symbols
- `cmd_explore` (`cai.py:7550`) — fully rewritten; now mirrors `cmd_spike` structure
- `cmd_spike` (`cai.py:7268`) — reference pattern; explore now follows it identically
- `LABEL_NEEDS_EXPLORATION` (`cai.py:200`) — kept; still drives issue selection
- `LABEL_EXPLORATION_DONE` — removed; no longer exists

## Design decisions
- Removed the two-phase (explore → human decision → follow-up) design entirely — it required `exploration-done` label which was causing the `'auto-improve:exploration-done' not found` error in issue #377
- Outcomes now mirror spike exactly: `close_documented`/`close_wont_do` → close, `refine_and_retry` → `:raised`, `## Refined Issue` → `:refined`, `## Exploration Blocked` → `:needs-human-review`
- Rejected: keeping the Phase 1 follow-up loop and just fixing the label creation — that would keep the broken two-phase design

## Out of scope / known gaps
- Existing `:exploration-done` issues in GitHub are orphaned; they should be manually relabelled to `:needs-exploration` to re-enter the new flow
- The `auto-improve:exploration-done` GitHub label itself is not deleted (GitHub keeps labels even when removed from publish.py)
- Pre-screen does not route to `:needs-exploration`; this is intentional (can be enhanced later)

## Invariants this change relies on
- `_write_active_job` and `_clear_active_job` are defined in cai.py and used identically by cmd_spike
- The cycle loop runs spike before explore when both exist (spike block comes first)
- `has_raised` check now gates on `not has_exploration` to match the same priority ordering as spike

## Revision 1 (2026-04-12)

### Rebase
- clean

### Files touched this revision
- `.claude/agents/cai-explore.md`:3 — updated frontmatter `description` to reflect new automated pipeline behavior (no longer "producing a structured report for human decision")

### Decisions this revision
- Used reviewer's suggested wording verbatim — it accurately captures the new single-phase, pipeline-integrated design

### New gaps / deferred
- None

## Revision 2 (2026-04-12)

### Rebase
- clean

### Files touched this revision
- `cai.py:7816` — updated cmd_cycle docstring loop description from `fix/spike` to `fix/spike/explore`

### Decisions this revision
- Applied reviewer's exact suggested wording verbatim — minimal one-word addition accurate to the new code behavior

### New gaps / deferred
- None
