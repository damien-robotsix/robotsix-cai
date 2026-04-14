# PR Context Dossier
Refs: damien-robotsix/robotsix-cai#563

## Files touched
- `cai_lib/config.py`:52 — deleted `LABEL_REQUESTED = "human:requested"`
- `cai_lib/__init__.py`:31,93 — removed `LABEL_REQUESTED` from import list and `__all__`
- `cai.py`:418 — removed `LABEL_REQUESTED: 6` from `_STATE_PRIORITY`
- `cai.py`:647-657 — updated `_select_fix_target` docstring to remove `human:requested` references
- `cai.py`:668 — changed for-loop tuple from `(LABEL_PLAN_APPROVED, LABEL_REQUESTED)` to `(LABEL_PLAN_APPROVED,)`
- `cai.py`:703-707 — updated plan-gate comment
- `cai.py`:718 — removed `LABEL_REQUESTED` from demotion `remove=` list
- `cai.py`:1053 — removed `origin_label: str` parameter from `_get_plan_for_fix`
- `cai.py`:1272-1275 — simplified plan-all yield check to single label count
- `cai.py`:1988 — deleted `origin_raised_label` variable assignment
- `cai.py`:2003,2013 — removed `LABEL_REQUESTED` from two more `remove=` lists
- `cai.py`:2044,2085,2256,2267 — replaced `{origin_raised_label}` f-strings with `{LABEL_PLAN_APPROVED}`
- `cai.py`:2130 — dropped second argument from `_get_plan_for_fix(issue)` call
- `cai.py`:2292-2300 — updated `terminal_remove` comment and list
- `cai.py`:7263,7269 — removed `LABEL_REQUESTED` from cmd_merge `remove=` lists
- `cai.py`:8490,8714 — updated inline comments
- `publish.py`:87 — removed `human:requested` from `LABELS` list
- `publish.py`:112-119 — added `LABELS_TO_DELETE` constant
- `publish.py`:328-336 — added deletion loop at end of `ensure_all_labels()`
- `.github/workflows/admin-only-label.yml`:22 — replaced fromJSON array: removed `human:requested` and `consistency:raised`, added `human:submitted`
- `.cai-staging/agents/cai-audit.md` — updated lifecycle state docs (3 lines)
- `README.md`:347-354 — rewrote human entry points paragraph
- `docs/architecture.md`:32,54 — removed `human:requested` references

## Files read (not touched) that matter
- `cai_lib/config.py` — source of LABEL_* constants
- `publish.py` — ensure_all_labels structure, LABELS list format

## Key symbols
- `LABEL_REQUESTED` (`cai_lib/config.py:52`) — deleted constant
- `_select_fix_target` (`cai.py:~640`) — for-loop reduced from 2 labels to 1
- `_get_plan_for_fix` (`cai.py:1053`) — signature simplified (removed `origin_label` param)
- `origin_raised_label` (`cai.py:1988`) — variable eliminated; replaced with `LABEL_PLAN_APPROVED` literal
- `ensure_all_labels` (`publish.py:305`) — now also deletes stale labels after creating current ones
- `LABELS_TO_DELETE` (`publish.py:112`) — new constant listing stale labels to idempotently delete

## Design decisions
- Kept `for label in (LABEL_PLAN_APPROVED,):` loop structure instead of converting to direct variable — avoids risky dedent of ~60 lines of loop body
- Used `replace_all=True` for the two cmd_merge lines (identical pattern on two lines)
- `cai-audit.md` update goes through `.cai-staging/agents/` because claude-code blocks direct edits to `.claude/agents/*.md`
- `LABELS_TO_DELETE` uses `check=False` because `gh label delete` exits non-zero when label is already absent (idempotent by design)

## Out of scope / known gaps
- Did not touch `cmd_refine`, `cmd_plan_all` logic — `human:submitted` already flows correctly through that path
- Did not touch `entrypoint.sh`, `docker-compose.yml`, `install.sh` — no references to `human:requested` there
- Did not touch `.github/workflows/` beyond `admin-only-label.yml`

## Invariants this change relies on
- `label_names` variable at old line 1987 is kept — still referenced to check `LABEL_IN_PROGRESS` / `LABEL_PR_OPEN` guards after it
- `_get_plan_for_fix` has exactly one call site (now at line ~2126); no other callers exist
- The `for label in (LABEL_PLAN_APPROVED,):` loop body is unchanged — no dedent needed
- `merge-blocked` (non-prefixed active label) is NOT in `LABELS_TO_DELETE`; only `auto-improve:merge-blocked` (stale prefixed variant) is
