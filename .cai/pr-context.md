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
