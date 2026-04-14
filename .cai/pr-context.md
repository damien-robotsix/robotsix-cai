# PR Context Dossier
Refs: robotsix-cai/robotsix-cai#619

## Files touched
- `.claude/agents/cai-fix.md` — deleted (14-line deprecated stub saying "renamed to cai-implement")

## Files read (not touched) that matter
- `.claude/agents/cai-fix.md` — confirmed it was a pure redirect stub before deletion

## Key symbols
- `cai-fix` (`.claude/agents/cai-fix.md`) — deprecated agent name, now removed

## Design decisions
- Used `git rm` to stage the deletion so the wrapper's `git add -A` picks it up cleanly
- No agent-memory deletion needed — `.claude/agent-memory/cai-fix/` does not exist in this clone

## Out of scope / known gaps
- `.claude/agent-memory/cai-fix/` referenced in issue does not exist; no action taken
- `cai-fix-ci.md` intentionally untouched — it is a distinct, active agent

## Invariants this change relies on
- No caller in `cai.py` or workflows uses `--agent cai-fix`; confirmed by Grep before deletion
