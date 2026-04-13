# PR Context Dossier
Refs: robotsix/robotsix-cai#572

## Files touched
- .claude/agents/cai-revise.md:1-340 — compressed from 340 → 179 lines by collapsing verbose prose into concise bullets and examples

## Files read (not touched) that matter
- .claude/agents/cai-revise.md — the only file changed; read in full before rewriting

## Key symbols
- `cai-revise.md` (.claude/agents/cai-revise.md:1) — agent definition for the revise subagent

## Design decisions
- Compressed "Self-modifying" section from ~65 lines to ~15 lines: kept the staging-directory mechanism and one GOOD/BAD example, dropped plugin example and full prose explanation
- Compressed "Working directory" section from ~30 lines to ~10 lines: kept one combined GOOD/BAD example, removed the 3 repetitive pairs
- Merged "Efficiency guidance" into "Hard rules — editing" as bullets 8–9: same guidance, fewer lines
- Converted "Hard rules — editing" long paragraphs to one-line bullets
- Rejected: removing rebase handling prose — issue explicitly says keep it
- Rejected: removing memory section — issue explicitly says keep it

## Out of scope / known gaps
- No other agent files touched (issue says "Do not touch other agent files")
- Content is preserved semantically; only prose density was reduced

## Invariants this change relies on
- All load-bearing guidance retained: rebase steps, staging-dir workaround, memory tracking, git-via-cai-git delegation
- The wrapper's staging-directory copy mechanism is unchanged and still works with the compressed instructions
