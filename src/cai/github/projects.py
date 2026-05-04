"""GitHub Projects V2 GraphQL client for cai.

Uses the GraphQL endpoint with the installation token, following the
same patterns established by ``pr.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

import requests

from .bot import CaiBot

_GRAPHQL_URL = "https://api.github.com/graphql"


@dataclass(frozen=True)
class ProjectField:
    """A single field (column) in a Projects V2 board."""

    id: str
    name: str
    typename: str


@dataclass(frozen=True)
class ProjectItem:
    """A single item (row) in a Projects V2 board."""

    id: str
    content_type: str
    content_title: str
    content_number: int | None
    field_values: dict[str, str]


def _graphql(bot: CaiBot, target_repo: str, query: str, variables: dict[str, object]) -> dict[str, object]:
    token = bot.token_for(target_repo)
    resp = requests.post(
        _GRAPHQL_URL,
        json={"query": query, "variables": variables},
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if "errors" in payload:
        raise RuntimeError(f"GraphQL error: {payload['errors']}")
    return payload["data"]


_LIST_ORG_PROJECTS_QUERY = """
query($login: String!) {
  organization(login: $login) {
    projectsV2(first: 50) {
      nodes {
        id
        title
        number
      }
    }
  }
}
"""


def list_org_projects(bot: CaiBot, org: str, target_repo: str) -> list[dict[str, object]]:
    """List Projects V2 in ``org``. Returns list with keys ``id``, ``title``, ``number``."""
    data = _graphql(bot, target_repo, _LIST_ORG_PROJECTS_QUERY, {"login": org})
    return data["organization"]["projectsV2"]["nodes"]


_GET_PROJECT_FIELDS_QUERY = """
query($projectId: ID!) {
  node(id: $projectId) {
    ... on ProjectV2 {
      fields(first: 50) {
        nodes {
          ... on ProjectV2FieldCommon {
            __typename
            id
            name
          }
        }
      }
    }
  }
}
"""


def get_project_fields(
    bot: CaiBot, target_repo: str, project_id: str
) -> list[ProjectField]:
    """Return all fields (columns) for the given project."""
    data = _graphql(
        bot, target_repo, _GET_PROJECT_FIELDS_QUERY, {"projectId": project_id}
    )
    nodes = data["node"]["fields"]["nodes"]
    return [
        ProjectField(id=n["id"], name=n["name"], typename=n["__typename"])
        for n in nodes
    ]


_GET_PROJECT_ITEMS_QUERY = """
query($projectId: ID!) {
  node(id: $projectId) {
    ... on ProjectV2 {
      items(first: 50) {
        nodes {
          id
          type
          content {
            __typename
            ... on Issue {
              title
              number
            }
            ... on PullRequest {
              title
              number
            }
            ... on DraftIssue {
              title
            }
          }
          fieldValues(first: 50) {
            nodes {
              __typename
              ... on ProjectV2ItemFieldTextValue {
                text
                field {
                  ... on ProjectV2FieldCommon {
                    name
                  }
                }
              }
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field {
                  ... on ProjectV2FieldCommon {
                    name
                  }
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


def _parse_field_values(field_value_nodes: list[dict[str, object]]) -> dict[str, str]:
    """Convert a list of field-value GraphQL nodes into a ``{name: value}`` dict."""
    result: dict[str, str] = {}
    for fv in field_value_nodes:
        typename = fv.get("__typename", "")
        field = fv.get("field")
        if not field:
            continue
        field_name = field.get("name")
        if not field_name:
            continue
        if typename == "ProjectV2ItemFieldTextValue":
            result[field_name] = fv.get("text") or ""
        elif typename == "ProjectV2ItemFieldSingleSelectValue":
            result[field_name] = fv.get("name") or ""
    return result


def get_project_items(
    bot: CaiBot, target_repo: str, project_id: str
) -> list[ProjectItem]:
    """Return all items (rows) for the given project."""
    data = _graphql(
        bot, target_repo, _GET_PROJECT_ITEMS_QUERY, {"projectId": project_id}
    )
    nodes = data["node"]["items"]["nodes"]
    items: list[ProjectItem] = []
    for n in nodes:
        content = n.get("content") or {}
        content_type = content.get("__typename") or n["type"]
        content_title = content.get("title") or ""
        content_number: int | None = content.get("number")
        field_values = _parse_field_values(n["fieldValues"]["nodes"])
        items.append(
            ProjectItem(
                id=n["id"],
                content_type=content_type,
                content_title=content_title,
                content_number=content_number,
                field_values=field_values,
            )
        )
    return items


_ADD_ITEM_MUTATION = """
mutation($projectId: ID!, $contentId: ID!) {
  addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
    item {
      id
    }
  }
}
"""


def add_item_to_project(
    bot: CaiBot, target_repo: str, project_id: str, content_id: str
) -> str:
    """Add an issue or PR to a project. Returns the new project-item node id."""
    data = _graphql(
        bot,
        target_repo,
        _ADD_ITEM_MUTATION,
        {"projectId": project_id, "contentId": content_id},
    )
    return data["addProjectV2ItemById"]["item"]["id"]


_SET_FIELD_VALUE_MUTATION = """
mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $value: ProjectV2FieldValue!) {
  updateProjectV2ItemFieldValue(
    input: {projectId: $projectId, itemId: $itemId, fieldId: $fieldId, value: $value}
  ) {
    projectV2Item {
      id
    }
  }
}
"""


def set_item_field_value(
    bot: CaiBot,
    target_repo: str,
    project_id: str,
    item_id: str,
    field_id: str,
    value: str,
    *,
    value_type: str = "text",
) -> None:
    """Update a field value on a project item.

    ``value_type`` controls how ``value`` is packaged:
    ``"text"`` → ``{text: value}``, ``"single_select"`` → ``{singleSelectOptionId: value}``.
    """
    if value_type == "single_select":
        value_payload = {"singleSelectOptionId": value}
    else:
        value_payload = {"text": value}

    _graphql(
        bot,
        target_repo,
        _SET_FIELD_VALUE_MUTATION,
        {
            "projectId": project_id,
            "itemId": item_id,
            "fieldId": field_id,
            "value": value_payload,
        },
    )
