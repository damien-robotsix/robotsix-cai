---
name: cost_query
description: Filter and aggregate cost log rows. Returns JSON. Pass filters as a JSON object in $ARGUMENTS.
user-invocable: false
allowed-tools: Read, Glob
arguments: [filters_json]
---

# cost_query — Cost Log Query Tool

Filter cost-log rows from `/var/log/cai/cai-cost.jsonl` (and the
aggregate dir when populated). Invoke this skill by passing a JSON
object containing optional filter keys.

To look up per-issue cost data (cost rows + outcome + PR-linked rows),
pass `{"issue_number": N}` — the function routes to `cost_issue()` and
returns the structured dict directly; all other parameters are ignored.

## Parameters (`$ARGUMENTS` = JSON object)

| Key | Type | Description |
|---|---|---|
| `issue_number` | integer | Route to `cost_issue(issue_number)`; returns per-issue structured dict; all other parameters are ignored |
| `agent` | string | Exact match on the `agent` field |
| `target` | integer | Exact match on `target_number` |
| `phase` | string | Exact match on `fsm_state` (e.g. `"IN_PROGRESS"`) |
| `module` | string | Exact match on `module` field |
| `session` | string | Exact match on `session_id` |
| `since` | string | ISO timestamp lower bound (inclusive), e.g. `"2026-01-01T00:00:00Z"` |
| `until` | string | ISO timestamp upper bound (exclusive) |
| `fingerprint` | string | Exact match on `prompt_fingerprint` |
| `min_cost` | float | Minimum `cost_usd` threshold (inclusive) |
| `group_by` | string | Group rows by this field; returns `{value: [rows]}` map |
| `last_n` | integer | Return only the last N rows (sorted by timestamp, takes precedence over since/until) |

## Return value

- **Default**: JSON array of matching cost-log row dicts. When `group_by` is
  set, returns a JSON object mapping distinct field values to arrays of rows.
  When `last_n` is set, returns the N most recent matching rows.
- **When `issue_number` is set**: returns the per-issue structured dict:
  ```json
  {
    "cost_rows": [...],
    "outcome": {...} | null,
    "linked_pr_rows": [...]
  }
  ```

## Instructions

1. Read `${CLAUDE_SKILL_DIR}/cost_audit.py` to understand the full
   implementation — it contains `cost_query()` and `cost_issue()`
   with complete filtering and grouping logic.

2. Parse `$ARGUMENTS` as JSON (or treat as empty `{}` if omitted).

3. Apply the `cost_query` logic from the implementation file:
   - If `issue_number` is set, call `cost_issue(issue_number)` and
     return the result directly (all other parameters are ignored).
   - Otherwise, load rows via the logic in `_load_rows()` (reads
     aggregate dir if present, falls back to
     `/var/log/cai/cai-cost.jsonl`).
   - Apply each non-None filter in order (agent, target, phase,
     module, session, since, until, fingerprint, min_cost).
   - If `group_by` is set, group rows into a `{value: [rows]}` dict.
   - If `last_n` is set (and no `group_by`), slice the last N rows.

4. Output the result as a single JSON value on stdout (array or
   object), with no surrounding prose.

## Example invocations

```
Skill(skill="cost_query", args='{"agent": "cai-implement", "last_n": 20}')
```

Returns the 20 most recent rows for `cai-implement`.

```
Skill(skill="cost_query", args='{"issue_number": 1208}')
```

Returns cost rows, outcome record, and PR-linked cost rows for issue #1208.
