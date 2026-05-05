"""GitHub Projects (v2) integration: tickets are project items, not issues.

When ``PROJECT_OWNER`` and ``PROJECT_NUMBER`` are set in ``app.env``,
agents file work as **draft project items** ("tickets") on the
configured Project instead of opening repo issues. Tickets carry:

- ``Type`` (single-select, required): ``code-change`` | ``analysis``.
- ``Status`` (single-select, required): see lifecycle below.
- ``Approved`` (single-select, optional): a single option named ``Yes``.
  Set by the user on a ticket in ``In Review`` to authorise auto-merge.
- ``Needs Rebase`` (single-select, optional): a single option named
  ``Yes``. Set by cai when a PR develops merge conflicts; the rebase
  cron resolves and clears the flag.

Lifecycle (Status):

  Backlog ─(refine)─▶ Refined ─(user)─▶ Ready ─(solve start)─▶ In Progress
                                                                  │
                                                                  ▼
                                                              In Review
                                                                  │
                                              ┌───────────────────┤
                                              ▼                   ▼
                                         Approved=Yes          Done
                                              │                  ▲
                                              └─(auto-merge)─────┘

Crons watch each transition trigger:
  - Status=Backlog   → cai-solve runs refine only, ends at Refined.
  - Status=Ready     → cai-solve runs Implement onwards.
  - Approved=Yes &
    Status=In Review → cai-merge merges the linked PR.
  - Needs Rebase=Yes → cai-rebase resolves conflicts, clears the flag.

Helpers in this module:

- :func:`is_enabled` — check for project config.
- :func:`get_issue_type` — read the ``Type`` field for an existing issue
  (legacy v0 routing).
- :func:`create_draft_ticket` — file a new draft on the project.
- :func:`list_tickets` / :func:`find_tickets_by_status` — pages of items.
- :func:`find_tickets_pending_action` — items where a flag field is set
  to ``Yes``, scoped by Status (used by merge/rebase crons).
- :func:`set_status` / :func:`set_type` / :func:`set_flag` — field updates.
- :func:`promote_ticket_to_issue` — convert a draft to a real GitHub
  issue in the configured default repo (used at code-change start so a
  PR can close the issue).

GraphQL surface: every helper goes through ``_graphql`` which posts to
``api.github.com/graphql`` with the App's installation token.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import requests
from github.Issue import Issue

from .bot import CaiBot

_TYPE_FIELD_NAME = "Type"
_STATUS_FIELD_NAME = "Status"
_APPROVED_FIELD_NAME = "Approved"
_NEEDS_REBASE_FIELD_NAME = "Needs Rebase"
_FLAG_OPTION_NAME = "Yes"  # single option on the boolean-ish flag fields

_TICKET_TYPES = ("code-change", "analysis")

# Status values, in canonical lifecycle order.
STATUS_BACKLOG = "Backlog"
STATUS_REFINED = "Refined"
STATUS_READY = "Ready"
STATUS_IN_PROGRESS = "In Progress"
STATUS_IN_REVIEW = "In Review"
STATUS_DONE = "Done"

ALL_STATUSES = (
    STATUS_BACKLOG,
    STATUS_REFINED,
    STATUS_READY,
    STATUS_IN_PROGRESS,
    STATUS_IN_REVIEW,
    STATUS_DONE,
)


def is_enabled(bot: CaiBot) -> bool:
    """``True`` when ``PROJECT_OWNER`` and ``PROJECT_NUMBER`` are configured."""
    return bool(bot.project_owner and bot.project_number)


@dataclass(frozen=True)
class ProjectMeta:
    project_id: str
    field_ids: dict[str, str]                    # field name -> field node id
    field_options: dict[str, dict[str, str]]     # field name -> {option name -> option id}


_meta_cache: dict[str, ProjectMeta] = {}


def _meta_cache_key(bot: CaiBot) -> str:
    return f"{bot.app_id}:{bot.project_owner}:{bot.project_number}"


def _clear_meta_cache() -> None:
    """Test hook — wipe the project-meta cache between cases."""
    _meta_cache.clear()


def _graphql(bot: CaiBot, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    """POST a GraphQL query/mutation. Returns the ``data`` dict, raising on any error."""
    repo_for_token = bot.project_default_repo or f"{bot.project_owner}/{bot.project_owner}"
    token = bot.token_for(repo_for_token)
    resp = requests.post(
        "https://api.github.com/graphql",
        json={"query": query, "variables": variables},
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("errors"):
        raise RuntimeError(
            f"GraphQL error from GitHub: {payload['errors']}"
        )
    return payload.get("data") or {}


_RESOLVE_QUERY_USER = """
query($owner: String!, $number: Int!) {
  user(login: $owner) {
    projectV2(number: $number) {
      id
      fields(first: 50) {
        nodes {
          ... on ProjectV2FieldCommon { id name }
          ... on ProjectV2SingleSelectField {
            id
            name
            options { id name }
          }
        }
      }
    }
  }
}
"""

_RESOLVE_QUERY_ORG = _RESOLVE_QUERY_USER.replace("user(login: $owner)", "organization(login: $owner)")


def _resolve_project_meta(bot: CaiBot) -> ProjectMeta:
    """Look up the project's node ID and the IDs of every field/option. Cached."""
    if not is_enabled(bot):
        raise RuntimeError(
            "Projects integration is not configured: set PROJECT_OWNER and "
            "PROJECT_NUMBER in app.env."
        )
    key = _meta_cache_key(bot)
    cached = _meta_cache.get(key)
    if cached is not None:
        return cached

    query = _RESOLVE_QUERY_ORG if bot.project_owner_type == "organization" else _RESOLVE_QUERY_USER
    data = _graphql(
        bot,
        query,
        {"owner": bot.project_owner, "number": bot.project_number},
    )
    container = data.get(bot.project_owner_type) or data.get("user") or data.get("organization")
    if not container or not container.get("projectV2"):
        raise RuntimeError(
            f"Project not found: owner={bot.project_owner!r} "
            f"number={bot.project_number} (owner_type={bot.project_owner_type!r})"
        )
    project = container["projectV2"]
    field_ids: dict[str, str] = {}
    field_options: dict[str, dict[str, str]] = {}
    for node in project.get("fields", {}).get("nodes") or []:
        if not node:
            continue
        name = node.get("name")
        node_id = node.get("id")
        if not name or not node_id:
            continue
        field_ids[name] = node_id
        opts = node.get("options")
        if opts:
            field_options[name] = {opt["name"]: opt["id"] for opt in opts}

    meta = ProjectMeta(
        project_id=project["id"],
        field_ids=field_ids,
        field_options=field_options,
    )
    _meta_cache[key] = meta
    return meta


def _ensure_required_fields(meta: ProjectMeta) -> None:
    """Validate the project schema before any ticket op runs."""
    missing = [name for name in (_TYPE_FIELD_NAME, _STATUS_FIELD_NAME) if name not in meta.field_ids]
    if missing:
        raise RuntimeError(
            f"Project is missing required single-select field(s): {missing}. "
            f"Add them to the configured project."
        )


# ---------------------------------------------------------------------------
# Legacy: read Type from an existing issue's project items (v0 routing).
# ---------------------------------------------------------------------------

_ISSUE_TYPE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      projectItems(first: 20) {
        nodes {
          fieldValues(first: 30) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field {
                  ... on ProjectV2SingleSelectField { name }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


def get_issue_type(bot: CaiBot, repo: str, number: int) -> str | None:
    """Return the value of the ``Type`` single-select field on any project the issue is on.

    Returns the option name (e.g. ``"analysis"``, ``"code-change"``) or
    ``None`` if no Type field is set / readable. The caller is expected to
    treat ``None`` as the default (``code-change``) flow.
    """
    owner, _, name = repo.partition("/")
    if not owner or not name:
        raise ValueError(f"expected owner/repo, got {repo!r}")
    token = bot.token_for(repo)
    resp = requests.post(
        "https://api.github.com/graphql",
        json={
            "query": _ISSUE_TYPE_QUERY,
            "variables": {"owner": owner, "name": name, "number": number},
        },
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        return None
    payload = resp.json()
    if payload.get("errors"):
        return None
    items = (
        payload.get("data", {})
        .get("repository", {})
        .get("issue", {})
        or {}
    ).get("projectItems", {}).get("nodes") or []
    for item in items:
        for value in (item.get("fieldValues") or {}).get("nodes") or []:
            field = value.get("field") or {}
            if field.get("name") == _TYPE_FIELD_NAME and value.get("name"):
                return value["name"]
    return None


# ---------------------------------------------------------------------------
# Ticket creation, listing, lifecycle.
# ---------------------------------------------------------------------------

_ADD_DRAFT_MUTATION = """
mutation($projectId: ID!, $title: String!, $body: String!) {
  addProjectV2DraftIssue(input: {projectId: $projectId, title: $title, body: $body}) {
    projectItem { id }
  }
}
"""

_SET_SINGLE_SELECT_MUTATION = """
mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $projectId,
    itemId: $itemId,
    fieldId: $fieldId,
    value: {singleSelectOptionId: $optionId}
  }) {
    projectV2Item { id }
  }
}
"""


def _set_single_select(
    bot: CaiBot, meta: ProjectMeta, item_id: str, field_name: str, option_name: str
) -> None:
    field_id = meta.field_ids.get(field_name)
    options = meta.field_options.get(field_name) or {}
    option_id = options.get(option_name)
    if not field_id or not option_id:
        raise RuntimeError(
            f"Project field {field_name!r} has no option {option_name!r}. "
            f"Available: {sorted(options)}"
        )
    _graphql(
        bot,
        _SET_SINGLE_SELECT_MUTATION,
        {
            "projectId": meta.project_id,
            "itemId": item_id,
            "fieldId": field_id,
            "optionId": option_id,
        },
    )


def create_draft_ticket(
    bot: CaiBot,
    title: str,
    body: str,
    type: Literal["code-change", "analysis"],
    status: str = "Backlog",
) -> str:
    """Create a draft project item with ``Type`` and ``Status`` set. Returns its node ID.

    Raises ``RuntimeError`` if the project isn't configured, the schema is
    missing required fields, or the type/status names don't match the
    project's options.
    """
    if type not in _TICKET_TYPES:
        raise ValueError(f"type must be one of {_TICKET_TYPES}, got {type!r}")
    meta = _resolve_project_meta(bot)
    _ensure_required_fields(meta)

    data = _graphql(
        bot,
        _ADD_DRAFT_MUTATION,
        {"projectId": meta.project_id, "title": title, "body": body},
    )
    item_id = data["addProjectV2DraftIssue"]["projectItem"]["id"]
    _set_single_select(bot, meta, item_id, _TYPE_FIELD_NAME, type)
    _set_single_select(bot, meta, item_id, _STATUS_FIELD_NAME, status)
    return item_id


def set_status(bot: CaiBot, item_id: str, status: str) -> None:
    """Move a ticket to ``status`` (e.g. ``"In Progress"``, ``"Done"``)."""
    meta = _resolve_project_meta(bot)
    _ensure_required_fields(meta)
    _set_single_select(bot, meta, item_id, _STATUS_FIELD_NAME, status)


def set_type(bot: CaiBot, item_id: str, type: Literal["code-change", "analysis"]) -> None:
    """Update a ticket's Type field. Used when refine reclassifies a draft."""
    if type not in _TICKET_TYPES:
        raise ValueError(f"type must be one of {_TICKET_TYPES}, got {type!r}")
    meta = _resolve_project_meta(bot)
    _ensure_required_fields(meta)
    _set_single_select(bot, meta, item_id, _TYPE_FIELD_NAME, type)


# ---------------------------------------------------------------------------
# Boolean-ish flag fields (Approved / Needs Rebase).
#
# ProjectsV2 has no native boolean type. We model these as single-select
# fields with one option named ``Yes``: presence of the value means the
# flag is set; clearing the field unsets it.
# ---------------------------------------------------------------------------

_CLEAR_FIELD_MUTATION = """
mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!) {
  clearProjectV2ItemFieldValue(input: {
    projectId: $projectId,
    itemId: $itemId,
    fieldId: $fieldId
  }) {
    projectV2Item { id }
  }
}
"""


def set_flag(bot: CaiBot, item_id: str, field_name: str, value: bool) -> None:
    """Set ``field_name`` to ``Yes`` (when ``value`` is True) or clear it.

    The field must be configured on the project as a single-select with at
    least an option named ``Yes``. ``field_name`` is typically
    ``"Approved"`` or ``"Needs Rebase"`` — exposed as named constants
    ``_APPROVED_FIELD_NAME`` / ``_NEEDS_REBASE_FIELD_NAME``.
    """
    meta = _resolve_project_meta(bot)
    field_id = meta.field_ids.get(field_name)
    if not field_id:
        raise RuntimeError(
            f"Project has no field {field_name!r}. Add it to use this flag."
        )
    if value:
        _set_single_select(bot, meta, item_id, field_name, _FLAG_OPTION_NAME)
    else:
        _graphql(
            bot,
            _CLEAR_FIELD_MUTATION,
            {
                "projectId": meta.project_id,
                "itemId": item_id,
                "fieldId": field_id,
            },
        )


# ---------------------------------------------------------------------------
# Ticket listing — used by the cron trigger.
# ---------------------------------------------------------------------------

_LIST_ITEMS_QUERY_USER = """
query($owner: String!, $number: Int!, $cursor: String) {
  user(login: $owner) {
    projectV2(number: $number) {
      items(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isArchived
          fieldValues(first: 30) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field { ... on ProjectV2SingleSelectField { name } }
              }
            }
          }
          content {
            __typename
            ... on DraftIssue { title body }
            ... on Issue { number url repository { nameWithOwner } title }
            ... on PullRequest { number url repository { nameWithOwner } title }
          }
        }
      }
    }
  }
}
"""

_LIST_ITEMS_QUERY_ORG = _LIST_ITEMS_QUERY_USER.replace(
    "user(login: $owner)", "organization(login: $owner)"
)


@dataclass(frozen=True)
class Ticket:
    item_id: str
    type: str | None
    status: str | None
    approved: bool
    needs_rebase: bool
    content_type: str   # "DraftIssue" | "Issue" | "PullRequest"
    title: str
    body: str
    issue_repo: str | None    # set when content is an Issue/PR
    issue_number: int | None  # set when content is an Issue/PR
    issue_url: str | None     # set when content is an Issue/PR


def _ticket_from_node(node: dict[str, Any]) -> Ticket | None:
    if not node or node.get("isArchived"):
        return None
    content = node.get("content") or {}
    content_type = content.get("__typename") or "Unknown"

    type_value: str | None = None
    status_value: str | None = None
    approved = False
    needs_rebase = False
    for value in (node.get("fieldValues") or {}).get("nodes") or []:
        field = value.get("field") or {}
        fname = field.get("name")
        vname = value.get("name")
        if fname == _TYPE_FIELD_NAME:
            type_value = vname
        elif fname == _STATUS_FIELD_NAME:
            status_value = vname
        elif fname == _APPROVED_FIELD_NAME and vname == _FLAG_OPTION_NAME:
            approved = True
        elif fname == _NEEDS_REBASE_FIELD_NAME and vname == _FLAG_OPTION_NAME:
            needs_rebase = True

    title = content.get("title") or ""
    body = content.get("body") or ""
    issue_repo = (content.get("repository") or {}).get("nameWithOwner")
    issue_number = content.get("number")
    issue_url = content.get("url")

    return Ticket(
        item_id=node["id"],
        type=type_value,
        status=status_value,
        approved=approved,
        needs_rebase=needs_rebase,
        content_type=content_type,
        title=title,
        body=body,
        issue_repo=issue_repo,
        issue_number=issue_number,
        issue_url=issue_url,
    )


def list_tickets(bot: CaiBot) -> list[Ticket]:
    """Page through every non-archived ticket on the configured project."""
    if not is_enabled(bot):
        return []
    query = _LIST_ITEMS_QUERY_ORG if bot.project_owner_type == "organization" else _LIST_ITEMS_QUERY_USER
    cursor: str | None = None
    out: list[Ticket] = []
    while True:
        data = _graphql(
            bot,
            query,
            {
                "owner": bot.project_owner,
                "number": bot.project_number,
                "cursor": cursor,
            },
        )
        container = data.get(bot.project_owner_type) or data.get("user") or data.get("organization") or {}
        items = (container.get("projectV2") or {}).get("items") or {}
        for node in items.get("nodes") or []:
            t = _ticket_from_node(node)
            if t is not None:
                out.append(t)
        page = items.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")
    return out


def find_tickets_by_status(bot: CaiBot, status: str) -> list[Ticket]:
    """Return tickets whose Status field equals ``status``. Empty when not configured."""
    return [t for t in list_tickets(bot) if t.status == status]


def find_tickets_pending_merge(bot: CaiBot) -> list[Ticket]:
    """Tickets in ``In Review`` with ``Approved=Yes`` — ready for the auto-merge cron."""
    return [
        t for t in list_tickets(bot)
        if t.status == STATUS_IN_REVIEW and t.approved
    ]


def find_tickets_pending_rebase(bot: CaiBot) -> list[Ticket]:
    """Tickets with ``Needs Rebase=Yes`` — ready for the rebase cron."""
    return [t for t in list_tickets(bot) if t.needs_rebase]


# ---------------------------------------------------------------------------
# Promotion: draft → real GitHub issue (so a PR can close it).
# ---------------------------------------------------------------------------

_REPO_ID_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) { id }
}
"""

_PROMOTE_MUTATION = """
mutation($itemId: ID!, $repositoryId: ID!) {
  convertProjectV2DraftIssueItemToIssue(input: {itemId: $itemId, repositoryId: $repositoryId}) {
    item {
      content {
        ... on Issue {
          number
          url
          repository { nameWithOwner }
        }
      }
    }
  }
}
"""


def promote_ticket_to_issue(
    bot: CaiBot, item_id: str, repo: str | None = None
) -> Issue:
    """Convert a draft project item into a real issue in ``repo``.

    If ``repo`` is None, ``PROJECT_DEFAULT_REPO`` is used. The project item
    keeps its field values and points at the new issue. Returns the
    ``github.Issue`` for the freshly-created issue.
    """
    if not is_enabled(bot):
        raise RuntimeError("Projects integration is not configured.")
    target_repo = repo or bot.project_default_repo
    if not target_repo:
        raise RuntimeError(
            "promote_ticket_to_issue: no target repo (set PROJECT_DEFAULT_REPO "
            "or pass repo=...)"
        )
    owner, _, name = target_repo.partition("/")
    repo_id_data = _graphql(bot, _REPO_ID_QUERY, {"owner": owner, "name": name})
    repository_id = (repo_id_data.get("repository") or {}).get("id")
    if not repository_id:
        raise RuntimeError(f"Could not resolve repository id for {target_repo!r}")

    data = _graphql(
        bot,
        _PROMOTE_MUTATION,
        {"itemId": item_id, "repositoryId": repository_id},
    )
    content = (
        (data.get("convertProjectV2DraftIssueItemToIssue") or {})
        .get("item", {})
        .get("content")
        or {}
    )
    issue_number = content.get("number")
    if not issue_number:
        raise RuntimeError("Promotion returned no issue number")
    return bot.repo(target_repo).get_issue(issue_number)
