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

## Parameters (`$ARGUMENTS` = JSON object)

| Key | Type | Description |
|---|---|---|
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

JSON array of matching cost-log row dicts (each row is a dict as
written by `log_cost`). When `group_by` is set, returns a JSON
object mapping distinct field values to arrays of matching rows.
When `last_n` is set, returns the N most recent matching rows.

## Instructions

1. Read `${CLAUDE_SKILL_DIR}/cost_audit.py` to understand the full
   implementation — it contains `cost_query()` and `cost_issue()`
   with complete filtering and grouping logic.

2. Parse `$ARGUMENTS` as JSON (or treat as empty `{}` if omitted).

3. Apply the `cost_query` logic from the implementation file:
   - Load rows via the logic in `_load_rows()` (reads aggregate dir
     if present, falls back to `/var/log/cai/cai-cost.jsonl`).
   - Apply each non-None filter in order (agent, target, phase,
     module, session, since, until, fingerprint, min_cost).
   - If `group_by` is set, group rows into a `{value: [rows]}` dict.
   - If `last_n` is set (and no `group_by`), slice the last N rows.

4. Output the result as a single JSON value on stdout (array or
   object), with no surrounding prose.

## Example invocation

```
Skill(skill="cost_query", args='{"agent": "cai-implement", "last_n": 20}')
```

Returns the 20 most recent rows for `cai-implement`.

---

# cost_issue — Per-Issue Cost + Outcome Tool

Return cost rows, outcome record, and PR-linked cost rows for a
specific issue number.

## Parameters (`$ARGUMENTS` = JSON object)

| Key | Type | Description |
|---|---|---|
| `issue_number` | integer | **Required.** The GitHub issue number to look up. |

## Return value

```json
{
  "cost_rows": [...],
  "outcome": {...} | null,
  "linked_pr_rows": [...]
}
```

- `cost_rows`: all cost-log rows where `target_number == issue_number`
- `outcome`: the outcome-log row for this issue (`/var/log/cai/cai-outcomes.jsonl`), or `null`
- `linked_pr_rows`: cost-log rows where `target_number` is a PR number linked to the issue (rows whose `pr_number` field matches any PR that has `target_number == issue_number`)

## Instructions

1. Read `${CLAUDE_SKILL_DIR}/cost_audit.py` for the full
   `cost_issue()` implementation.

2. Parse `$ARGUMENTS` as JSON to get `issue_number`.

3. Apply the `cost_issue` logic:
   - Load all cost rows (same `_load_rows()` helper).
   - Filter `cost_rows`: rows where `target_number == issue_number`.
   - Load outcome log (`/var/log/cai/cai-outcomes.jsonl`); find the
     row where `issue_number == n` (the most recent if multiple).
   - Find PR-linked rows: rows where `target_number` is in the set
     of PR numbers that also appear with `target_number == issue_number`
     in cost rows (i.e. same target chain), OR where `pr_number == issue_number`.
   - Return the structured JSON object.

4. Output the JSON object on stdout with no surrounding prose.

## Example invocation

```
Skill(skill="cost_query", args='{"issue_number": 1208}')
```

> **Note:** both tools share this single skill entry. When calling
> `cost_issue`, pass `{"issue_number": N}` and the skill will detect
> the `issue_number` key and run the `cost_issue` code path instead
> of `cost_query`.
