"""GitHub Projects V2 GraphQL helpers for cai-solve.

Thin wrappers around the GitHub GraphQL API for managing project items,
fields, and single-select field values.
"""

from __future__ import annotations

import requests

from .bot import CaiBot

_GRAPHQL_URL = "https://api.github.com/graphql"


def _graphql(bot: CaiBot, query: str, variables: dict, repo: str) -> dict:
    token = bot.token_for(repo)
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


_GET_PROJECT_ID_QUERY = """
query($owner: String!, $number: Int!) {
  organization(login: $owner) { projectV2(number: $number) { id } }
  user(login: $owner) { projectV2(number: $number) { id } }
}
"""


def get_project_id(bot: CaiBot, repo: str, owner: str, project_number: int) -> str:
    data = _graphql(bot, _GET_PROJECT_ID_QUERY, {"owner": owner, "number": project_number}, repo)
    org_id = (data.get("organization") or {}).get("projectV2") or {}
    user_id = (data.get("user") or {}).get("projectV2") or {}
    result = org_id.get("id") or user_id.get("id")
    if result is None:
        raise ValueError(f"Project #{project_number} not found for owner {owner!r}")
    return result


_GET_ISSUE_NODE_ID_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { issue(number: $number) { id } }
}
"""


def get_issue_node_id(bot: CaiBot, repo: str, number: int) -> str:
    owner, name = repo.split("/", 1)
    data = _graphql(bot, _GET_ISSUE_NODE_ID_QUERY, {"owner": owner, "name": name, "number": number}, repo)
    issue = (data.get("repository") or {}).get("issue")
    if issue is None:
        raise ValueError(f"Issue #{number} not found in {repo!r}")
    return issue["id"]


_ADD_ITEM_MUTATION = """
mutation($projectId: ID!, $contentId: ID!) {
  addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
    item { id }
  }
}
"""


def add_item_to_project(bot: CaiBot, repo: str, project_id: str, content_node_id: str) -> str:
    data = _graphql(
        bot,
        _ADD_ITEM_MUTATION,
        {"projectId": project_id, "contentId": content_node_id},
        repo,
    )
    return data["addProjectV2ItemById"]["item"]["id"]


_GET_FIELDS_QUERY = """
query($projectId: ID!) {
  node(id: $projectId) {
    ... on ProjectV2 {
      fields(first: 50) {
        nodes {
          ... on ProjectV2SingleSelectField { id name options { id name } }
          ... on ProjectV2Field { id name }
        }
      }
    }
  }
}
"""


def get_project_fields(bot: CaiBot, repo: str, project_id: str) -> dict[str, dict[str, object]]:
    data = _graphql(bot, _GET_FIELDS_QUERY, {"projectId": project_id}, repo)
    nodes = data["node"]["fields"]["nodes"]
    result: dict[str, dict[str, object]] = {}
    for field in nodes:
        entry: dict[str, object] = {"id": field["id"]}
        options = field.get("options")
        if options is not None:
            entry["options"] = {opt["name"]: opt["id"] for opt in options}
        result[field["name"]] = entry
    return result


_SET_SINGLE_SELECT_MUTATION = """
mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $value: ProjectV2FieldValue!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $projectId, itemId: $itemId, fieldId: $fieldId, value: $value
  }) { clientMutationId }
}
"""


def set_single_select_field(
    bot: CaiBot,
    repo: str,
    project_id: str,
    item_id: str,
    field_id: str,
    option_id: str,
) -> None:
    _graphql(
        bot,
        _SET_SINGLE_SELECT_MUTATION,
        {
            "projectId": project_id,
            "itemId": item_id,
            "fieldId": field_id,
            "value": {"singleSelectOptionId": option_id},
        },
        repo,
    )
