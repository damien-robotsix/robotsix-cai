---
title: Workflow Registry
parent: Workflows
nav_order: 0
---

# Workflow Registry

`src/cai/workflows/registry.py` is the single source of truth for every cai CLI
workflow. Each entry in the `WORKFLOWS` list describes one user-facing `cai-*`
command — its metadata drives docs generation, CI YAML generation, GitHub event
routing, and Langfuse session tracking.

## `WorkflowSpec`

A frozen dataclass (`WorkflowSpec`) captures every piece of metadata downstream
tooling needs:

| Field | Type | Purpose |
|---|---|---|
| `slug` | `str` | URL-safe identifier (`"solve"`, `"audit"`). Used for doc filenames and CI YAML names. |
| `title` | `str` | Human-readable title (`"CAI Solve"`). Appears in Just-the-Docs nav and CI workflow `name`. |
| `nav_order` | `int` | Position in the "Workflows" nav. Must be unique across the registry. |
| `blurb` | `str` | Description rendered at the top of the generated docs page. |
| `graph` | `pydantic_graph.Graph` | The `Graph` instance whose nodes and edges produce the mermaid diagram. |
| `cli_entry` | `str` | Import path (`"cai.workflows.solve:main"`) for the CLI entry point. Must be listed in `[project.scripts]` in `pyproject.toml`. |
| `session_id` | `Callable[..., str]` | Callable that returns a Langfuse session ID (see [Langfuse session ID](#langfuse-session-id)). |
| `github_trigger` | `GitHubTrigger` | Trigger configuration for the generated GitHub Actions YAML (`on:` block and optional `job_if`). |
| `docker_command` | `str` | Command run inside the container by the CI job. May include `${{ }}` expressions. |
| `permissions` | `dict[str, str]` | GitHub Actions job permissions (e.g. `{"contents": "write"}`). Must be non-empty. |
| `concurrency_group` | `str \| None` | Concurrency group expression to prevent overlapping runs. `None` when no concurrency control is needed. |
| `authorized_user_variant` | `str` | Controls which users can trigger the workflow. One of: `"standard"`, `"skip_bots"`, `"none"`. |

Supporting types:

- **`GitHubTrigger`** — holds an `on` list of `GitHubTriggerEvent` objects plus an optional `job_if` conditional for the job-level guard.
- **`GitHubTriggerEvent`** — one trigger event (`"issues"`, `"workflow_dispatch"`, `"schedule"`, `"push"`, `"workflow_run"`, or `"pull_request_review"`) with optional `types`, `branches`, `workflows`, `cron`, and `inputs`.

## Generators

Two scripts consume the registry. Both run as pre-commit hooks (`.pre-commit-config.yaml`) so generated files stay in sync automatically.

### `scripts/gen_workflow_graphs.py`

Produces two kinds of docs under `docs/workflows/`:

- **`index.md`** — the parent page with `has_children: true`, listing all child pages automatically via Just-the-Docs.
- **`{slug}.md`** — one page per spec, containing the blurb and a mermaid diagram rendered from `spec.graph.mermaid_code()`.

Every commit that touches `src/cai/workflows/` re-runs this script; if the output changes, pre-commit fails, re-stages the regenerated files, and the commit proceeds with up-to-date diagrams.

### `scripts/gen_github_workflows.py`

Produces `.github/workflows/cai-{slug}.yml` for every spec by rendering the Jinja template `scripts/templates/cai_workflow.yml.j2` with the spec's fields.

Workflows whose CI YAML needs to be hand-written (or should not exist) can be skipped by adding their slug to the `SKIP_SLUGS` set at the top of this script. Currently: `{"audit-auto", "memory-audit"}`.

## Langfuse session ID

The `session_id` field is a **callable** — not a string — so session IDs are
resolved at run time with access to contextual data like issue numbers and
branch names.

- **`_solve_session_id(number, branch=None)`** — returns `"issue-{number}"` for issue runs. When a branch is supplied (PR path), delegates to `session_id_for_pr` in `src/cai/log/observability.py`.
- **`_audit_session_id()`** — returns `"audit-{YYYYMMDD-HHMMSS}"`.
- **`_sourcing_session_id()`** — returns `"sourcing-{YYYYMMDD-HHMMSS}"`.
- **`_memory_audit_session_id()`** — returns `"memory-audit-{YYYYMMDD-HHMMSS}"`.
- **`session_id_for_pr(pr_number, branch)`** — matches `cai/solve-{issue}` branch names to group PR reviews under the original issue session; falls back to `"pr-{n}"` for human-created PRs.

This grouping means an issue-solving run, its resulting PR's review-thread runs, and any later conflict resolution all share one Langfuse session.

## How to add a new workflow

Follow these steps to wire a new `cai-*` CLI through the registry.

### 1. Write the graph

Create a `pydantic_graph.Graph` with your `BaseNode` subclasses. Model it after
the existing graphs in `src/cai/workflows/` (e.g. `solve_graph` in `fsm.py`,
`audit_graph` in `audit.py`).

### 2. Add a `WorkflowSpec` entry

Insert a `WorkflowSpec(...)` into the `WORKFLOWS` list in
`src/cai/workflows/registry.py`. Fill every field:

```python
WorkflowSpec(
    slug="my-workflow",
    title="CAI My Workflow",
    nav_order=9,                        # next unused number
    blurb="Short description of what this workflow does.",
    graph=my_workflow_graph,
    cli_entry="cai.workflows.my_workflow:main",
    session_id=_my_workflow_session_id,
    github_trigger=GitHubTrigger(
        on=[GitHubTriggerEvent(event="workflow_dispatch")],
    ),
    docker_command="cai-my-workflow",
    permissions={"contents": "read"},
)
```

- `nav_order` must be unique — pick the next available integer.
- `slug` must be unique — it determines the doc filename and CI YAML name.
- `session_id` must be a callable returning a `str`.
- `permissions` must be a non-empty `dict[str, str]`.
- `authorized_user_variant` must be `"standard"`, `"skip_bots"`, or `"none"`.

### 3. Add the CLI entry point

Add an entry to `[project.scripts]` in `pyproject.toml`:

```toml
cai-my-workflow = "cai.workflows.my_workflow:main"
```

### 4. Skip GitHub Actions generation (only if needed)

If your workflow should not have a generated CI YAML (or needs a hand-written one), add its slug to `SKIP_SLUGS` in `scripts/gen_github_workflows.py`. Otherwise the generator handles it automatically — no extra step required.

### 5. Run the generators

```bash
python scripts/gen_workflow_graphs.py
python scripts/gen_github_workflows.py
```

Or commit and let the pre-commit hooks regenerate the files. Either way, the generators produce:

- `docs/workflows/my-workflow.md`
- `.github/workflows/cai-my-workflow.yml`

### 6. Commit everything

Commit the registry entry, the graph module, the `pyproject.toml` change, and all generated files.

## Sub-graphs are not in the registry

Modules like `src/cai/workflows/explore.py`, `refine.py`, and `implement.py`
define `BaseNode` subclasses that compose `solve_graph` in `fsm.py`. They are
internal graph nodes — not standalone CLI entry points — and are intentionally
excluded from the registry. The registry only catalogs top-level graphs that
map to a `cai-*` command.

## Tests

`tests/workflows/test_registry.py` verifies registry invariants. Adding a new
spec requires these tests to still pass. Key assertions:

- Unique `slug` values
- Unique `nav_order` values
- Every `cli_entry` is importable via `importlib`
- Every `session_id` is callable
- Every `permissions` dict is non-empty with string keys and values
- Every `authorized_user_variant` is one of `{"standard", "skip_bots", "none"}`
- Every `graph` is a `pydantic_graph.Graph` instance
- Every `github_trigger.on` list is non-empty
- Every `docker_command` is a non-empty string
