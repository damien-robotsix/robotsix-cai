"""One-shot Project bootstrap: schema fields + initial v1-rollout tickets.

Run this after:
  1. Creating an empty Project (v2) on the user/org account in the GitHub UI.
  2. Setting PROJECT_OWNER / PROJECT_NUMBER / PROJECT_OWNER_TYPE /
     PROJECT_DEFAULT_REPO in /home/cai/.config/cai/app.env (install.sh
     does this interactively; or edit the file directly).

The script is idempotent — re-running it skips fields/tickets that
already exist by name.

What it does:
  - Adds the required schema fields (Type, Status, Approved, Needs
    Rebase) to the Project, with the canonical option sets.
  - If the default ``Status`` field exists with the wrong options,
    deletes it before adding the canonical one.
  - Files the five v1-rollout tickets (#3-#7 in the local task list)
    as ``Backlog`` drafts.

Usage::

    python scripts/bootstrap_project.py
"""
from __future__ import annotations

import json
import sys
import textwrap

import requests

from cai import CaiBot
from cai.github import projects as projects_mod


_FIELD_SCHEMA = [
    ("Type", [
        ("code-change", "BLUE"),
        ("analysis", "PURPLE"),
    ]),
    ("Status", [
        ("Backlog", "GRAY"),
        ("Refined", "YELLOW"),
        ("Ready", "GREEN"),
        ("In Progress", "BLUE"),
        ("In Review", "ORANGE"),
        ("Done", "GREEN"),
    ]),
    ("Approved", [
        ("Yes", "GREEN"),
    ]),
    ("Needs Rebase", [
        ("Yes", "RED"),
    ]),
]


# Tickets to seed — title, type, body. Status defaults to Backlog.
_SEED_TICKETS = [
    (
        "cai-solve --ticket entrypoint + cron polling workflow",
        "code-change",
        textwrap.dedent("""\
            Add a `--ticket <item-id>` mode to `cai-solve` that:

            - Reads the project ticket by node ID via `projects.list_tickets`
              (or a dedicated `get_ticket(item_id)` lookup).
            - Branches on `ticket.type`:
              - `code-change`: if the ticket is a draft, call
                `promote_ticket_to_issue(bot, item_id)`. Then run the existing
                `solve_graph` against the resulting issue. The Langfuse
                `session_id` MUST be the project item ID (not the issue
                number) so the trace lifecycle stays unified.
              - `analysis`: build an `IssueState` directly from the ticket
                body (no issue), run `Explore → Refine → Comment` only.

            Add a polling workflow `.github/workflows/cai-solve-tickets.yml`:
            cron every ~5 min, lists tickets by Status:
            - Status=Backlog → run `cai-solve --ticket` for refine-only pass
              (ends at Status=Refined).
            - Status=Ready   → run `cai-solve --ticket` for implement-onwards
              pass (ends at Status=In Review for code-change, Status=Done
              for analysis).

            Use a matrix strategy so multiple Ready tickets run in parallel.

            ## Verification
            - Smoke-test by manually moving a ticket to Ready and watching
              the cron pick it up.
        """),
    ),
    (
        "Audit/sourcing migration to project tickets",
        "code-change",
        textwrap.dedent("""\
            Migrate `src/cai/workflows/audit.py` and `sourcing.py` so they
            file work as project tickets instead of GitHub issues.

            ## Plan

            1. Rename `ProposedIssue` → `ProposedTicket`. Add a `type:
               Literal["code-change", "analysis"]` field with description
               coaching the audit agent on when each fits.
            2. Rename `_create_issues_from_proposals` → `_create_tickets_from_proposals`.
               Body stays mostly the same — replace `repo.create_issue(...)`
               with `projects.create_draft_ticket(...)` (Backlog status).
            3. The issue-deduplicator subagent currently reads existing
               issues to avoid dupes. Update it to read project tickets
               via `list_tickets(bot)` and dedupe on title similarity.
            4. Update audit/sourcing agent prompts to emit `type` per
               proposal and to know they're filing tickets, not issues.
               Each `*_auditor.md` agent's output schema needs the new
               field.
            5. Tests in `tests/workflows/test_audit.py`, `test_sourcing.py`,
               `test_audit_tools.py` — replace issue-creation assertions
               with ticket-creation assertions.

            ## Out of scope
            - Backfilling already-closed issues into tickets.
        """),
    ),
    (
        "Refine decomposition migration to sub-tickets",
        "code-change",
        textwrap.dedent("""\
            `RefineNode.run` currently spawns sub-issues via `push()`. With
            the project flow, decomposed sub-tasks should be sub-tickets.

            ## Plan

            1. `RefineOutput.sub_issues: list[str]` → `sub_tickets:
               list[SubTicket]` where `SubTicket` has `title`, `type`, and
               an optional one-line `summary`. Refine emits a typed sub-task
               (so child workflow doesn't need to re-classify).
            2. `RefineNode.run` decomposition path:
               - For each sub-ticket, write the body to
                 `<parent>/sub_<n>.md` as today.
               - Call `projects.create_draft_ticket(title=..., type=...,
                 status="Backlog")` with that body. (Drop the GitHub
                 sub-issue linkage — project items aren't linked to issues.
                 Track parentage via a custom "Parent" text field if we add
                 one later.)
            3. Update `refine.md` agent prompt: `## Decomposition` section
               teaches the agent to emit typed sub-tickets and to perform
               analysis work inline rather than spawning analysis sub-tickets
               (one of the original v0 failure modes).
            4. Tests in `tests/workflows/test_refine.py` switch from
               sub-issue assertions to sub-ticket assertions.

            ## Verification
            - Refine a parent ticket whose plan spans two layers; confirm
              two sub-tickets land in Backlog with the right types.
        """),
    ),
    (
        "Lifecycle: Status updates during cai-solve",
        "code-change",
        textwrap.dedent("""\
            Wire status transitions through the workflow so the project
            board reflects work in flight.

            ## Transitions

            | When | Status |
            |---|---|
            | Refine ends (analysis or sub-ticket-decomposing) | Refined |
            | cai-solve --ticket starts implement | In Progress |
            | PR opened (PRNode end) | In Review |
            | PR merged or analysis comment posted | Done |
            | Solve raises an exception | Status untouched; ticket stays in In Progress for triage |

            ## Plan

            1. New helper `cai.workflows.tickets` module with
               `set_ticket_status(bot, ticket_id, status)` thin wrapper —
               idempotent, no-op when ticket_id is None (so non-ticket
               solve invocations stay backwards-compatible).
            2. Threading: `IssueState` gets a `ticket_id: str | None` field
               populated from the `--ticket` invocation. Each transition
               point calls `set_ticket_status`.
            3. RefineNode: when `state.flow_kind == "code-change"` and a
               ticket_id is set, set Status=Refined and End the graph
               (instead of returning ImplementNode). The Ready→Implement
               cron will resume from there.
            4. ImplementNode start: set Status=In Progress.
            5. PRNode end (when `state.pr_url` is populated): Status=In Review.
            6. CommentNode end: Status=Done.

            ## Tests
            - `test_fsm.py`: verify ticket_id round-trips into state.
            - Per-node tests: assert set_ticket_status called with the
              right value when ticket_id is set.

            Depends on tickets #1 (cai-solve --ticket entrypoint) and #3
            (Refine sub-ticket migration).
        """),
    ),
    (
        "Approved=Yes auto-merge cron + Needs Rebase=Yes rebase cron",
        "code-change",
        textwrap.dedent("""\
            Two periodic workflows that close the project lifecycle loop.

            ## Auto-merge cron (Approved=Yes)

            New `.github/workflows/cai-merge-approved.yml` running every
            ~5 min:

            1. `projects.find_tickets_pending_merge(bot)` returns tickets
               in Status=In Review with Approved=Yes.
            2. For each, find the PR linked by ticket.issue_number /
               ticket.issue_url. Merge via `gh pr merge --squash --auto`
               (or pygithub).
            3. On merge success: `set_status(bot, item_id, "Done")` and
               `set_flag(bot, item_id, "Approved", False)`.
            4. On merge conflict: `set_flag(bot, item_id, "Needs Rebase",
               True)` so the rebase cron picks it up.

            ## Rebase cron (Needs Rebase=Yes)

            New `.github/workflows/cai-rebase.yml` running every ~10 min:

            1. `projects.find_tickets_pending_rebase(bot)`.
            2. For each, locate the PR's head branch, run the existing
               `cai-resolve-conflicts` flow.
            3. On success: `set_flag(bot, item_id, "Needs Rebase", False)`.
            4. On failure: post a comment on the PR with the conflict log;
               leave the flag set so a human can intervene.

            ## Edge cases
            - Ticket with Approved=Yes but no linked PR (e.g., human
              flipped the flag prematurely): warn + skip.
            - PR already merged: clear flags + set Status=Done.

            Depends on ticket #1 (cai-solve --ticket entrypoint) since
            the lifecycle hooks share helpers.
        """),
    ),
]


# ---------------------------------------------------------------------------


def _gql(bot: CaiBot, query: str, variables: dict | None = None) -> dict:
    token = bot.token_for(bot.project_default_repo or f"{bot.project_owner}/{bot.project_owner}")
    r = requests.post(
        "https://api.github.com/graphql",
        json={"query": query, "variables": variables or {}},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=30,
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("errors"):
        raise SystemExit(f"GraphQL error:\n{json.dumps(payload['errors'], indent=2)}")
    return payload.get("data") or {}


def _delete_field(bot: CaiBot, field_id: str) -> None:
    _gql(
        bot,
        "mutation($id: ID!) {"
        "  deleteProjectV2Field(input: {fieldId: $id}) { projectV2Field { ... on ProjectV2FieldCommon { id } } }"
        "}",
        {"id": field_id},
    )


def _create_field(bot: CaiBot, project_id: str, name: str, options: list[tuple[str, str]]) -> None:
    opts_payload = [
        {"name": opt_name, "color": color, "description": ""}
        for opt_name, color in options
    ]
    _gql(
        bot,
        "mutation($projectId: ID!, $name: String!, $opts: [ProjectV2SingleSelectFieldOptionInput!]!) {"
        "  createProjectV2Field(input: {"
        "    projectId: $projectId, dataType: SINGLE_SELECT, name: $name, singleSelectOptions: $opts"
        "  }) { projectV2Field { ... on ProjectV2SingleSelectField { id name } } }"
        "}",
        {"projectId": project_id, "name": name, "opts": opts_payload},
    )


def ensure_schema(bot: CaiBot) -> None:
    """Add any missing fields. Replaces the default Status field if its options don't match."""
    projects_mod._clear_meta_cache()
    meta = projects_mod._resolve_project_meta(bot)
    print(f"Project: {meta.project_id}")
    print(f"  existing fields: {sorted(meta.field_ids)}")

    expected_status = {opt for opt, _ in dict(_FIELD_SCHEMA)["Status"]}

    for name, options in _FIELD_SCHEMA:
        if name in meta.field_ids:
            existing = set(meta.field_options.get(name, {}))
            wanted = {opt for opt, _ in options}
            if existing == wanted:
                print(f"  {name}: OK")
                continue
            print(f"  {name}: options mismatch (have={sorted(existing)}, want={sorted(wanted)}) — recreating")
            _delete_field(bot, meta.field_ids[name])
        else:
            print(f"  {name}: missing — creating")
        _create_field(bot, meta.project_id, name, options)

    projects_mod._clear_meta_cache()


def seed_tickets(bot: CaiBot) -> None:
    existing = {t.title for t in projects_mod.list_tickets(bot)}
    for title, type_, body in _SEED_TICKETS:
        if title in existing:
            print(f"ticket exists, skipping: {title}")
            continue
        item_id = projects_mod.create_draft_ticket(
            bot, title=title, body=body, type=type_, status="Backlog"
        )
        print(f"created [{type_}] {item_id}  {title}")


def main() -> None:
    bot = CaiBot()
    if not projects_mod.is_enabled(bot):
        sys.exit(
            "PROJECT_OWNER / PROJECT_NUMBER not set in app.env. Run "
            "install.sh and choose 'Configure GitHub Projects integration', "
            "or edit /home/cai/.config/cai/app.env directly."
        )
    print(f"Bootstrapping project {bot.project_owner}/{bot.project_number} "
          f"(owner_type={bot.project_owner_type})")
    ensure_schema(bot)
    seed_tickets(bot)
    print("\nDone.")


if __name__ == "__main__":
    main()
