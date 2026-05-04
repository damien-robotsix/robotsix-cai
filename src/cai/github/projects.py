"""GitHub Projects V2 GraphQL helpers for cai-solve.

Uses the GraphQL endpoint with installation tokens, following the pattern
established in ``cai.github.pr``.
"""
from __future__ import annotations

from .bot import CaiBot
from .pr import _graphql


def _owner_name(repo: str) -> tuple[str, str]:
    owner, _, name = repo.partition("/")
    return owner, name


_GET_ISSUE_NODE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      id
    }
  }
}
"""


def get_issue_node_id(bot: CaiBot, repo: str, issue_number: int) -> str:
    owner, name = _owner_name(repo)
    data = _graphql(bot, _GET_ISSUE_NODE_QUERY,
                    {"owner": owner, "name": name, "number": issue_number})
    issue = data["repository"]["issue"]
    if issue is None:
        raise ValueError(f"Issue #{issue_number} not found in {repo}")
    return issue["id"]


_GET_PROJECT_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  organization(login: $owner) {
    projectV2(number: $number) {
      id
    }
  }
}
"""


def get_project_id(bot: CaiBot, repo: str, project_number: int) -> str:
    owner, name = _owner_name(repo)
    data = _graphql(bot, _GET_PROJECT_QUERY,
                    {"owner": owner, "name": name, "number": project_number})
    project = data["organization"]["projectV2"]
    if project is None:
        raise ValueError(f"Project #{project_number} not found in org {owner}")
    return project["id"]


_ADD_ITEM_MUTATION = """
mutation($projectId: ID!, $contentId: ID!,
         $owner: String!, $name: String!) {
  addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
    item {
      id
    }
  }
}
"""


def add_item_to_project(
    bot: CaiBot, repo: str, project_id: str, content_id: str
) -> str:
    owner, name = _owner_name(repo)
    data = _graphql(bot, _ADD_ITEM_MUTATION,
                    {"projectId": project_id, "contentId": content_id,
                     "owner": owner, "name": name})
    return data["addProjectV2ItemById"]["item"]["id"]


_GET_STATUS_FIELD_QUERY = """
query($projectId: ID!, $owner: String!, $name: String!) {
  node(id: $projectId) {
    ... on ProjectV2 {
      fields(first: 20) {
        nodes {
          ... on ProjectV2SingleSelectField {
            id
            name
            options {
              id
              name
            }
          }
        }
      }
    }
  }
}
"""

_SET_STATUS_MUTATION = """
mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $value: ProjectV2FieldValue!,
         $owner: String!, $name: String!) {
  updateProjectV2ItemFieldValue(
    input: {projectId: $projectId, itemId: $itemId, fieldId: $fieldId, value: $value}
  ) {
    projectV2Item { id }
  }
}
"""


def set_status(
    bot: CaiBot, repo: str, project_id: str, item_id: str, status: str
) -> None:
    owner, name = _owner_name(repo)
    # 1. Find the Status field and its options
    data = _graphql(bot, _GET_STATUS_FIELD_QUERY,
                    {"projectId": project_id, "owner": owner, "name": name})
    fields = data["node"]["fields"]["nodes"]
    status_field = None
    status_option = None
    for field in fields:
        if field.get("name") == "Status":
            status_field = field
            for opt in field["options"]:
                if opt["name"] == status:
                    status_option = opt
                    break
            break
    if status_field is None:
        raise ValueError(f"No 'Status' field found on project {project_id}")
    if status_option is None:
        raise ValueError(
            f"Status '{status}' not found on project {project_id}. "
            f"Available: {[o['name'] for o in status_field['options']]}"
        )
    # 2. Set the value
    _graphql(bot, _SET_STATUS_MUTATION, {
        "projectId": project_id,
        "itemId": item_id,
        "fieldId": status_field["id"],
        "value": {"singleSelectOptionId": status_option["id"]},
        "owner": owner,
        "name": name,
    })
