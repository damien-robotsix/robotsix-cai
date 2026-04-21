# scripts

Maintenance shell scripts and the coverage verifier, invoked
manually or from CI. `scripts/generate-index.sh` regenerates
`CODEBASE_INDEX.md`; `scripts/generate-fsm-docs.py` regenerates
`docs/fsm.md`; `scripts/server-cleanup.sh` does age/size cleanup
on the transcript-sync store; `scripts/check-modules-coverage.py`
verifies every tracked file is matched by exactly one module in
`docs/modules.yaml`.

## Key entry points
- [`scripts/generate-index.sh`](../../scripts/generate-index.sh) —
  generator for `CODEBASE_INDEX.md`. Embeds a one-line
  description per tracked file; edit descriptions here, not in
  the generated markdown.
- [`scripts/generate-fsm-docs.py`](../../scripts/generate-fsm-docs.py)
  — generator for `docs/fsm.md`. Renders the `ISSUE_TRANSITIONS`
  and `PR_TRANSITIONS` tables from `cai_lib.fsm` as Mermaid
  diagrams.
- [`scripts/server-cleanup.sh`](../../scripts/server-cleanup.sh) —
  server-side age/size cleanup for the transcript-sync store
  (runs on the OVH box, not inside the container).
- [`scripts/check-modules-coverage.py`](../../scripts/check-modules-coverage.py)
  — module-coverage verifier. Calls
  `cai_lib.audit.modules.load_modules` + `coverage_check` over
  `git ls-files`; exit 1 on any error.

## Inter-module dependencies
- Imports from **audit** — `check-modules-coverage.py` imports
  `load_modules` and `coverage_check` from
  `cai_lib.audit.modules`.
- Imports from **fsm** — `generate-fsm-docs.py` imports the
  `ISSUE_TRANSITIONS` / `PR_TRANSITIONS` tables.
- Writes **docs** — the two generator scripts own
  `CODEBASE_INDEX.md` and `docs/fsm.md`.
- Run by **actions** — the `REVIEWING_DOCS` FSM handler
  (`cai_lib/actions/review_docs.py`) invokes both generators and
  `check-modules-coverage.py` on every PR that reaches pre-merge
  docs review; drift is bundled into the handler's final commit.
- No reverse imports from pipeline code.

## Operational notes
- **Generator invariants.** Neither generated file should be
  hand-edited; descriptions for `CODEBASE_INDEX.md` live in
  `generate-index.sh`, and `docs/fsm.md` is pure-render. PRs that
  hand-edit these files will see their changes overwritten by
  the next `REVIEWING_DOCS` FSM pass.
- **Server-cleanup scope.** `server-cleanup.sh` runs outside the
  container on the SSH endpoint; adjusting its schedule requires
  changes on the host, not in this repo.
- **Coverage script contract.** Every tracked file must match
  exactly one module glob in `docs/modules.yaml`; stray files
  break `check-modules-coverage.py` and the `cai-review-docs`
  stage.
- **Cost sensitivity.** Zero — pure shell / Python.
- **CI implications.** The `REVIEWING_DOCS` FSM handler depends
  on both generators staying idempotent and on
  `check-modules-coverage.py` exiting 0 on a valid tree;
  non-idempotent generator output produces phantom drift commits
  every PR, and an overly strict coverage check blocks every PR
  until module narratives catch up.
