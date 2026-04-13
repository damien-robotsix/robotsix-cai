# PR Context Dossier
Refs: robotsix/robotsix-cai#573

## Files touched
- `cai.py:5931` — added `headRefName` to `gh pr view` JSON fields for cai-review-pr
- `cai.py:5945` — added `headRefName` to `gh pr list` JSON fields for cai-review-pr
- `cai.py:5964` — extract `branch = pr.get("headRefName", "")` in cai-review-pr loop
- `cai.py:5991-6024` — replaced diff fetch + shallow main clone with `gh repo clone` + PR branch checkout + `git diff --stat` for cai-review-pr
- `cai.py:6044-6049` — replaced `## PR diff` block with stat summary + exploration instruction in cai-review-pr user message
- `cai.py:6269-6281` — removed diff fetch from cai-review-docs (was lines 6262-6281 pre-edit)
- `cai.py:6291-6305` — added `git diff origin/main..HEAD --stat` computation in cai-review-docs
- `cai.py:6320-6326` — replaced `## PR diff` block with stat summary + exploration instruction in cai-review-docs user message
- `cai.py:6918-6924` — added 40k char truncation for cai-merge diff
- `cai.py:6941` — renamed `## PR diff` → `## PR changes` in cai-merge user message
- `.cai-staging/agents/cai-review-pr.md` — updated "What you receive" (stat not diff) and "How to work" (read files from clone)
- `.cai-staging/agents/cai-review-docs.md` — updated "What you receive" (stat not diff) and "How to work" (read files from clone)
- `.cai-staging/agents/cai-merge.md` — updated description, section name, added truncation note

## Files read (not touched) that matter
- `.claude/agents/cai-revise.md` — model for the stat-only approach (already uses `git diff --stat`)
- `cai.py:3281-3350` — cai-revise's stat computation and user message construction (template followed)

## Key symbols
- `pr_stat` (cai.py:6022, 6304) — stat output replacing the inlined diff
- `_MERGE_MAX_DIFF_LEN` (cai.py:6919) — 40k char cap for cai-merge diff truncation
- `branch` (cai.py:5964) — PR branch name now extracted in cai-review-pr loop

## Design decisions
- cai-review-pr: changed from shallow main clone to full `gh repo clone` + PR branch checkout — necessary to allow `git diff origin/main..HEAD --stat` and for agents to read PR-state files
- cai-review-docs: diff fetch removed entirely; stat computed from existing clone+checkout
- cai-merge: kept inline diff but added 40k char truncation and renamed heading — cai-merge is inline-only with no tools, changing it to use a clone is architectural (option a from issue) and deferred
- cai-confirm: `#### Merged PR diff` heading uses 4 hashes so it does not match `## PR diff`; already truncated to 8000 chars; no change needed

## Out of scope / known gaps
- cai-confirm and cai-merge giving them full tool access + clone (option a) is deferred — architectural change
- cai-confirm: already has MAX_DIFF_LEN=8000 truncation; heading has 4 hashes so grep check already passes without changes

## Invariants this change relies on
- `gh repo clone` + `git fetch origin <branch>` + `git checkout <branch>` produces a working tree where `git diff origin/main..HEAD --stat` shows the PR's changes
- cai-review-pr already has `--add-dir <work_dir>` so Read/Grep/Glob work on the clone
- cai-review-docs already has `--add-dir <work_dir>` for the same reason
