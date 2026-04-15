# PR Context Dossier
Refs: damien-robotsix/robotsix-cai#624

## Files touched
- `cai_lib/config.py`:115 — added `_STALE_APPLYING_HOURS = 2`
- `cai_lib/watchdog.py`:17-28 — imported LABEL_APPLYING, LABEL_RAISED, _STALE_APPLYING_HOURS; added LABEL_APPLYING to rollback loop; added TTL branch and rollback-to-RAISED logic
- `cai_lib/__init__.py`:47-49 — exported LABEL_APPLYING, LABEL_APPLIED, _STALE_APPLYING_HOURS
- `cai.py`:2489-2615 — added cmd_maintain function (lists :applying issues, clones repo, runs cai-maintain agent, applies FSM transition)
- `cai.py`:2700-2740 — added Phase 1.5 (drain :applied → :solved) and Phase 3 (run maintain if :applying issues exist) to _cmd_cycle_inner
- `cai.py`:3319-3330 — added "maintain" subparser with --issue
- `cai.py`:3380 — added "maintain": cmd_maintain to handlers dict
- `.cai-staging/agents/cai-maintain.md` — new agent file (frontmatter: model: sonnet, tools: Bash, Read)
- `tests/test_maintain.py` — new test file: happy path (HIGH), low/medium confidence divert, empty queue
- `tests/test_rollback.py` — added test_rollback_applying_stale (TTL=2h, 3h age → rollback) and test_rollback_applying_fresh (1h age → no rollback)

## Files read (not touched) that matter
- `cai_lib/fsm.py` — confirmed APPLYING/APPLIED states and transitions already present from step 2 (#636)
- `cai_lib/dispatcher.py` — confirmed APPLYING/APPLIED NOT in dispatcher registry (correct; handled by explicit cycle step)
- `tests/test_dispatcher.py` — confirmed test expects APPLYING/APPLIED absent from dispatcher actionable states
- `tests/test_fsm.py` — confirmed test_no_orphan_states already passes (APPLYING/APPLIED reachable via TRIAGING)
- `publish.py` — confirmed auto-improve:applying and auto-improve:applied labels already present

## Key symbols
- `cmd_maintain` (`cai.py`:2492) — main driver: picks oldest :applying issue, clones, runs agent, applies FSM transition
- `_rollback_stale_in_progress` (`cai_lib/watchdog.py`:33) — now handles LABEL_APPLYING with 2h TTL, rolling back to LABEL_RAISED
- `_cmd_cycle_inner` (`cai.py`:2670) — Phase 1.5 drains :applied → :solved; Phase 3 calls cmd_maintain if :applying queue non-empty
- `cai-maintain` agent (`.cai-staging/agents/cai-maintain.md`) — reads Ops: block, executes gh CLI ops, emits Confidence

## Design decisions
- APPLYING/APPLIED NOT registered in dispatcher — handled via explicit `_cmd_cycle_inner` steps, consistent with plan
- Rollback for APPLYING goes to LABEL_RAISED (not LABEL_REFINED) — maintenance issues come from triage, not the refine pipeline
- `cmd_maintain` uses local imports for `_work_directory_block`, `apply_transition_with_confidence`, `parse_confidence` — follows existing pattern in cai.py where these are not top-level imports
- Rejected: registering APPLYING in dispatcher — would conflict with test_dispatcher expectations and the explicit cycle integration

## Out of scope / known gaps
- `test_no_orphan_states` seeded_states exclusion: NOT needed (APPLYING reachable from RAISED via TRIAGING)
- publish.py labels were already present from step 2 (#636); not modified
- fsm.py states/transitions were already present from step 2 (#636); not modified

## Invariants this change relies on
- `cai_lib/fsm.py` already has APPLYING/APPLIED states and all three transitions (from step 2 #636)
- `publish.py` already has label entries for applying/applied (from step 2 #636)
- APPLYING is NOT in the dispatcher registry (test_dispatcher.py asserts this)
- The BFS test (test_no_orphan_states) already passes because triaging_to_applying exists
