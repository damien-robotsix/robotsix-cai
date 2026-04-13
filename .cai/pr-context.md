# PR Context Dossier
Refs: robotsix/robotsix-cai#377

## Files touched
- `.claude/agents/cai-plan.md`:5 — changed `model: claude-opus-4-6` to `model: claude-sonnet-4-6`

## Files read (not touched) that matter
- `.claude/agents/cai-plan.md` — the agent definition being changed

## Key symbols
- `model` frontmatter field (`.claude/agents/cai-plan.md`:5) — controls which Claude model the cai-plan agent uses

## Design decisions
- Used staging directory (`/.cai-staging/agents/cai-plan.md`) because `.claude/agents/*.md` files are write-blocked in headless `-p` mode
- Haiku delegation guidance (lines 71–80) was already present and correct — no changes needed there
- Rejected: modifying `cai.py` budget cap — explicitly out of scope per issue guardrails

## Out of scope / known gaps
- `--max-budget-usd 1.00` cap in `cai.py` left unchanged (independent safety rail)
- `cai-select.md` model unchanged — separate agent, separate issue
- Single-plan architecture (run 1 plan instead of 2) — medium risk, explicitly out of scope

## Invariants this change relies on
- The wrapper copies `.cai-staging/agents/*.md` to `.claude/agents/` by basename after the session exits
- The `cai-select` agent (still on Opus) provides a quality safety net when comparing two Sonnet-generated plans

## Revision 1 (2026-04-13)

### Rebase
- clean

### Files touched this revision
- `.claude/agents/cai-plan.md`:5 — reverted `model: claude-sonnet-4-6` back to `model: claude-opus-4-6` (via staging dir)
- `docs/agents.md`:17 — reverted cai-plan model column from `sonnet` back to `opus`

### Decisions this revision
- Reverted model from sonnet to opus per documented reviewer decision (memory line 110: "we should keep opus for planning the solution")
- Haiku delegation guidance (lines 71–80) and "3 or fewer files" boundary retained — reviewer had accepted those changes
- The review-docs agent comment was already addressed in the branch (docs/agents.md model sonnet), but since model is reverting to opus, the docs must also revert

### New gaps / deferred
- review-docs comment (opus→sonnet in docs/agents.md) was already applied before this revision; this revision undoes that since the model is reverting to opus
