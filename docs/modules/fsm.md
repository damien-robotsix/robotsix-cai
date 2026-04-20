# fsm

FSM core — enums for issue/PR states, the transition-data tables
that power the dispatcher, the `Confidence` parser that agents emit
in their output, and the Mermaid renderer used by the docs
generator. Implementation is split across three focused files so
the umbrella `fsm.py` re-exporter stays trivial; handlers always
import from `cai_lib.fsm` rather than the split modules directly.

## Key entry points
- [`cai_lib/fsm_states.py`](../../cai_lib/fsm_states.py) —
  `IssueState` (RAISED, REFINED, PLANNED, IN_PROGRESS, …) and
  `PRState` (OPEN, REVIEWING_CODE, REVIEWING_DOCS, APPROVED,
  CI_FAILING, REVISION_PENDING, REBASING, …). Enum values are the
  GitHub label suffixes, so `str(state)` round-trips through
  labels.
- [`cai_lib/fsm_transitions.py`](../../cai_lib/fsm_transitions.py) —
  `Transition` dataclass; `ISSUE_TRANSITIONS` and `PR_TRANSITIONS`
  tables; `get_issue_state`, `get_pr_state`, `find_transition`,
  `apply_transition`, `apply_transition_with_confidence`,
  `resume_transition_for`, `apply_pr_transition`,
  `apply_pr_transition_with_confidence`, `resume_pr_transition_for`,
  `render_fsm_mermaid` (library-backed via
  `transitions.extensions.GraphMachine`; the Mermaid source is
  post-processed to strip the library's YAML front matter and restore
  the `≥HIGH` / `caller-gated` display labels).
- [`cai_lib/fsm_confidence.py`](../../cai_lib/fsm_confidence.py) —
  `Confidence` enum (HIGH, MEDIUM, LOW, STOP);
  `parse_confidence`, `parse_confidence_reason`,
  `parse_resume_target`.
- [`cai_lib/fsm.py`](../../cai_lib/fsm.py) — umbrella re-exporter;
  the canonical import path for handlers.
- [`scripts/generate-fsm-docs.py`](../../scripts/generate-fsm-docs.py)
  — regenerates `docs/fsm.md` by calling `render_fsm_mermaid` over
  both transition tables. Before rendering it runs
  `_validate_catalog` on each catalog, which builds a
  `transitions.Machine` and surfaces unknown state references as
  `ValueError` — so catalog typos fail the docs-regen CI job instead
  of silently landing in the rendered diagram.

## Inter-module dependencies
- Imported by **actions** — every handler in `cai_lib/actions/*.py`
  reads the current state and applies a transition.
- Imported by **cli** — `dispatcher.py` routes on `IssueState` /
  `PRState`; `cmd_rescue`, `cmd_unblock`, `cmd_cycle` query
  transitions.
- Imported by **github-glue** — `watchdog.py` rolls back orphaned
  `:in-progress` / `:revising` labels using the helpers here.
- Imported by **tests** — `tests/test_fsm.py` pins
  `ISSUE_TRANSITIONS`, `PR_TRANSITIONS`, and the parse helpers;
  `tests/test_fsm_schema.py` validates both catalogs against
  `transitions.Machine` so unknown state references fail CI before
  the docs are regenerated; `tests/test_dispatcher.py` exercises the
  routing tables.
- No upstream imports inside the pipeline — this is a leaf
  dependency.
- `scripts/generate-fsm-docs.py` writes regenerated diagrams into
  **docs** (`docs/fsm.md`).

## Operational notes
- **Invariants.** A single issue must carry exactly one state label
  from the `IssueState` enum (same for PR ↔ `PRState`). The
  dispatcher enforces this through `apply_transition`; setting a
  label by hand can leave the FSM in an unreachable state.
- **Confidence parsing.** `parse_confidence` looks for
  `Confidence: HIGH|MEDIUM|LOW|STOP` in agent output; missing or
  malformed confidence is treated as `STOP` and diverts to
  `:human-needed`. Preserve this safe default.
- **CI implications.** Whenever a transition is added or renamed,
  `scripts/generate-fsm-docs.py` must re-run (handled
  automatically by the `regenerate-docs.yml` workflow on PRs).
  `tests/test_fsm.py` will fail if a transition reference goes
  stale.
- **Cost sensitivity.** Zero — pure Python with no Claude
  invocations.
