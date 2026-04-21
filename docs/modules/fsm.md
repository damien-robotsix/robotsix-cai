# fsm

FSM core — enums for issue/PR states, the transition-data tables
that power the dispatcher, the `Confidence` parser that agents emit
in their output, and the Mermaid renderer used by the docs
generator. Implementation is split across three focused files so
the umbrella `fsm.py` re-exporter stays trivial; handlers always
import from `cai_lib.fsm` rather than the split modules directly.

## Key entry points
- [`cai_lib/fsm_states.py`](../../cai_lib/fsm_states.py) —
  `IssueState` (RAISED, REFINED, SPLITTING, PLANNED, IN_PROGRESS,
  …) and `PRState` (OPEN, REVIEWING_CODE, REVIEWING_DOCS,
  APPROVED, CI_FAILING, REVISION_PENDING, REBASING, …). Enum
  values are the GitHub label suffixes, so `str(state)`
  round-trips through labels. `SPLITTING` sits between `REFINED`
  and `PLANNING`: after refine writes a structured plan, the
  dispatcher hands the issue to `cai-split` for atomic-vs-decompose
  evaluation; atomic verdicts advance to `PLANNING` (cai-plan),
  decompose verdicts create sub-issues and park the parent at
  `:parent`, LOW confidence diverts to `:human-needed`.
- [`cai_lib/fsm_transitions.py`](../../cai_lib/fsm_transitions.py) —
  `Transition` dataclass; `ISSUE_TRANSITIONS` and `PR_TRANSITIONS`
  tables; `get_issue_state`, `get_pr_state`, `find_transition`,
  `fire_trigger` (the canonical FSM dispatch entry point),
  `resume_transition_for`, `resume_pr_transition_for`,
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
- [`cai_lib/admin_sigils.py`](../../cai_lib/admin_sigils.py) —
  admin comment sigil scanner + processor. Currently supports
  `<!-- cai-resplit -->`, which rolls a `:plan-approved` issue back
  to `:refined` so `cai-split` re-evaluates scope on the next tick.
  Wired into Phase 0.7 of `cmd_cycle`; the sigil check is a literal-
  string match, no Haiku / Claude invocation.
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
  dispatcher enforces this through `fire_trigger`; setting a
  label by hand can leave the FSM in an unreachable state.
- **Confidence parsing.** `parse_confidence` looks for
  `Confidence: HIGH|MEDIUM|LOW|STOP` in agent output; missing or
  malformed confidence is treated as `STOP` and diverts to
  `:human-needed`. Preserve this safe default.
- **CI implications.** Whenever a transition is added or renamed,
  `scripts/generate-fsm-docs.py` must re-run (handled
  automatically by the `REVIEWING_DOCS` FSM handler in
  `cai_lib/actions/review_docs.py` on PRs). `tests/test_fsm.py`
  will fail if a transition reference goes stale.
- **Cost sensitivity.** Zero — pure Python with no Claude
  invocations.

## Admin comment sigils

A short, deterministic bypass channel for admins who notice a plan
needs a course correction without waiting for `cai-implement` to bail
or `cai rescue` to divert. Detected on every `cai cycle` tick by the
Phase 0.7 sweep in `cmd_cycle` via
[`cai_lib/admin_sigils.py`](../../cai_lib/admin_sigils.py).

| Sigil | Required label | Authoring identity | Effect |
|---|---|---|---|
| `<!-- cai-resplit -->` | `auto-improve:plan-approved` | Latest admin comment author (from `CAI_ADMIN_LOGINS`) | Fires `plan_approved_to_refined` — moves the issue to `:refined` so the dispatcher routes it to `handle_split` for a fresh atomic-vs-decompose verdict. An ack comment is posted on success. |

Rules:

- The sigil must appear in the **most recent** admin-authored comment
  on the issue — a later admin comment without the sigil means the
  admin has moved past the re-split intent and the scan ignores it.
- The sigil is a literal-string match; no Haiku classifier is invoked.
- Non-admin commenters echoing the sigil string never trigger the
  rollback (admin identity is checked via `cai_lib.config.is_admin_login`).
- The sigil does not add or clear any GitHub label — no
  `_ALL_MANAGED_ISSUE_LABELS` update is required.
