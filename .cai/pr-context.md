# PR Context Dossier
Refs: robotsix/robotsix-cai#720

## Files touched
- `cai_lib/fsm.py`:65-98 — added `_CONFIDENCE_REASON_RE` regex and `parse_confidence_reason` helper
- `cai_lib/actions/plan.py`:156-188 — extended `_SELECT_JSON_SCHEMA` with required `confidence_reason` field
- `cai_lib/actions/plan.py`:186-254 — updated `_run_select_agent` to return 3-tuple `(plan_text, confidence, reason)`
- `cai_lib/actions/plan.py`:256-303 — updated `_run_plan_select_pipeline` to return 3-tuple and unpack accordingly
- `cai_lib/actions/plan.py`:400-412 — updated `handle_plan` to unpack 3-tuple, store reason in plan block, stash on issue dict
- `cai_lib/actions/plan.py`:486-511 — updated `handle_plan_gate` to import/use `parse_confidence_reason` and pass `reason_extra=`
- `tests/test_fsm.py`:14 — imported `parse_confidence_reason`
- `tests/test_fsm.py`:122-157 — added 4 unit tests for `parse_confidence_reason`
- `.cai-staging/agents/cai-select.md` — added `confidence_reason` as required field in JSON schema section

## Files read (not touched) that matter
- `cai_lib/fsm.py`:620-650 — `_render_human_divert_reason` already appends `reason_extra` to divert comment; no changes needed there
- `cai_lib/fsm.py`:653-737 — `apply_transition_with_confidence` already accepts `reason_extra` param; only needed to wire it up

## Key symbols
- `parse_confidence_reason` (`cai_lib/fsm.py`:86) — extracts `Confidence reason: …` line from plan block; mirrors `parse_confidence`
- `_CONFIDENCE_REASON_RE` (`cai_lib/fsm.py`:65) — regex anchored to line start, case-insensitive
- `_SELECT_JSON_SCHEMA` (`cai_lib/actions/plan.py`:156) — now requires `confidence_reason`
- `_run_select_agent` (`cai_lib/actions/plan.py`:186) — now returns 3-tuple including reason
- `plan_confidence_reason` stored as `Confidence reason: <text>` line in plan block adjacent to `Confidence: <level>` line

## Design decisions
- `confidence_reason` made required in schema so model always provides it; for HIGH this becomes a brief confirmation
- Stored as a plain text line `Confidence reason: …` in the plan block (not markdown) to match `Confidence:` convention and enable simple regex extraction
- `handle_plan_gate` reads reason from both in-process stash and body parse for cross-process safety, mirroring how `plan_confidence` is handled
- Rejected: storing as a separate metadata comment block — adds complexity and is harder to parse

## Out of scope / known gaps
- `cai-merge`, `cai-triage`, `cai-unblock` do not yet surface `confidence_reason`; the issue explicitly excludes them
- No integration test added (manual trigger required per issue spec)

## Invariants this change relies on
- `apply_transition_with_confidence`'s `reason_extra` param already appends to the divert comment (`cai_lib/fsm.py`:643-644)
- The plan block delimiter `<!-- cai-plan-start -->` / `<!-- cai-plan-end -->` is unchanged
- `note` semantics (prepended blockquote for fix agent) are unchanged
