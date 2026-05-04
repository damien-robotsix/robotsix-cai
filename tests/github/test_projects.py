"""Tests for cai.github.projects — Projects V2 GraphQL client."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from unittest.mock import MagicMock, patch

import pytest
import requests

from cai.github.projects import (
    ProjectField,
    ProjectItem,
    _graphql,
    _parse_field_values,
    add_item_to_project,
    get_project_fields,
    get_project_items,
    list_org_projects,
    set_item_field_value,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bot() -> MagicMock:
    """A CaiBot mock that returns a fake token."""
    b = MagicMock()
    b.token_for.return_value = "fake-token"
    return b


# ---------------------------------------------------------------------------
# ProjectField
# ---------------------------------------------------------------------------


def test_project_field_creation():
    f = ProjectField(id="PF_1", name="Status", typename="ProjectV2Field")
    assert f.id == "PF_1"
    assert f.name == "Status"
    assert f.typename == "ProjectV2Field"


def test_project_field_frozen():
    f = ProjectField(id="PF_1", name="Status", typename="ProjectV2Field")
    with pytest.raises(FrozenInstanceError):
        f.name = "Priority"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ProjectItem
# ---------------------------------------------------------------------------


def test_project_item_creation():
    item = ProjectItem(
        id="PI_1",
        content_type="Issue",
        content_title="Fix bug",
        content_number=42,
        field_values={"Status": "Done", "Priority": "High"},
    )
    assert item.id == "PI_1"
    assert item.content_type == "Issue"
    assert item.content_title == "Fix bug"
    assert item.content_number == 42
    assert item.field_values == {"Status": "Done", "Priority": "High"}


def test_project_item_number_none():
    item = ProjectItem(
        id="PI_2",
        content_type="DraftIssue",
        content_title="Draft item",
        content_number=None,
        field_values={},
    )
    assert item.content_number is None


def test_project_item_frozen():
    item = ProjectItem(
        id="PI_1",
        content_type="Issue",
        content_title="Fix bug",
        content_number=42,
        field_values={},
    )
    with pytest.raises(FrozenInstanceError):
        item.content_title = "New title"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# _graphql
# ---------------------------------------------------------------------------


@patch("cai.github.projects.requests.post")
def test_graphql_uses_target_repo_for_auth(mock_post, bot):
    """_graphql calls bot.token_for with the explicit target_repo parameter."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"data": {"some": "payload"}}
    mock_post.return_value = mock_resp

    result = _graphql(bot, "acme/widgets", "query { foo }", {"login": "acme"})

    assert result == {"some": "payload"}
    bot.token_for.assert_called_once_with("acme/widgets")
    mock_post.assert_called_once()
    call_args, call_kwargs = mock_post.call_args
    assert call_args[0] == "https://api.github.com/graphql"
    assert call_kwargs["json"]["query"] == "query { foo }"
    assert call_kwargs["json"]["variables"] == {"login": "acme"}


@patch("cai.github.projects.requests.post")
def test_graphql_passes_correct_headers(mock_post, bot):
    """_graphql sets Authorization, Accept, and X-GitHub-Api-Version headers."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"data": {}}
    mock_post.return_value = mock_resp

    _graphql(bot, "x/y", "q", {})

    headers = mock_post.call_args[1]["headers"]
    assert headers["Authorization"] == "Bearer fake-token"
    assert headers["Accept"] == "application/vnd.github+json"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"


@patch("cai.github.projects.requests.post")
def test_graphql_raises_on_http_error(mock_post, bot):
    """_graphql raises HTTPError via raise_for_status()."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = requests.HTTPError("500 Server Error")
    mock_post.return_value = mock_resp

    with pytest.raises(requests.HTTPError, match="500 Server Error"):
        _graphql(bot, "x/y", "q", {})


@patch("cai.github.projects.requests.post")
def test_graphql_raises_on_errors_key(mock_post, bot):
    """_graphql raises RuntimeError when the response contains 'errors'."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {"errors": [{"message": "bad query"}]}
    mock_post.return_value = mock_resp

    with pytest.raises(RuntimeError, match="GraphQL error"):
        _graphql(bot, "x/y", "q", {})


# ---------------------------------------------------------------------------
# list_org_projects
# ---------------------------------------------------------------------------


@patch("cai.github.projects.requests.post")
def test_list_org_projects_returns_nodes(mock_post, bot):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "data": {
            "organization": {
                "projectsV2": {
                    "nodes": [
                        {"id": "PVT_1", "title": "Sprint Board", "number": 1},
                        {"id": "PVT_2", "title": "Roadmap", "number": 2},
                    ]
                }
            }
        }
    }
    mock_post.return_value = mock_resp

    result = list_org_projects(bot, "my-org", "my-org/some-repo")

    assert len(result) == 2
    assert result[0] == {"id": "PVT_1", "title": "Sprint Board", "number": 1}
    assert result[1] == {"id": "PVT_2", "title": "Roadmap", "number": 2}

    # Verify correct GraphQL variables
    call_args, call_kwargs = mock_post.call_args
    assert call_kwargs["json"]["variables"] == {"login": "my-org"}


@patch("cai.github.projects.requests.post")
def test_list_org_projects_empty(mock_post, bot):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "data": {"organization": {"projectsV2": {"nodes": []}}}
    }
    mock_post.return_value = mock_resp

    result = list_org_projects(bot, "my-org", "my-org/repo")

    assert result == []


# ---------------------------------------------------------------------------
# get_project_fields
# ---------------------------------------------------------------------------


_MIXED_FIELDS_RESPONSE = {
    "data": {
        "node": {
            "fields": {
                "nodes": [
                    {"__typename": "ProjectV2Field", "id": "PF_1", "name": "Title"},
                    {
                        "__typename": "ProjectV2SingleSelectField",
                        "id": "PF_2",
                        "name": "Status",
                    },
                    {"__typename": "ProjectV2Field", "id": "PF_3", "name": "Priority"},
                ]
            }
        }
    }
}


@patch("cai.github.projects.requests.post")
def test_get_project_fields_mixed(mock_post, bot):
    mock_resp = MagicMock()
    mock_resp.json.return_value = _MIXED_FIELDS_RESPONSE
    mock_post.return_value = mock_resp

    result = get_project_fields(bot, "o/r", "PVT_1")

    assert len(result) == 3
    assert result[0] == ProjectField("PF_1", "Title", "ProjectV2Field")
    assert result[1] == ProjectField("PF_2", "Status", "ProjectV2SingleSelectField")
    assert result[2] == ProjectField("PF_3", "Priority", "ProjectV2Field")

    call_args, call_kwargs = mock_post.call_args
    assert call_kwargs["json"]["variables"] == {"projectId": "PVT_1"}


@patch("cai.github.projects.requests.post")
def test_get_project_fields_empty(mock_post, bot):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"data": {"node": {"fields": {"nodes": []}}}}
    mock_post.return_value = mock_resp

    result = get_project_fields(bot, "o/r", "PVT_1")

    assert result == []


# ---------------------------------------------------------------------------
# get_project_items
# ---------------------------------------------------------------------------


_ITEMS_RESPONSE = {
    "data": {
        "node": {
            "items": {
                "nodes": [
                    {
                        "id": "PVTI_1",
                        "type": "ISSUE",
                        "content": {
                            "__typename": "Issue",
                            "title": "Fix bug",
                            "number": 42,
                        },
                        "fieldValues": {
                            "nodes": [
                                {
                                    "__typename": "ProjectV2ItemFieldTextValue",
                                    "text": "Fix login bug",
                                    "field": {"name": "Title"},
                                },
                                {
                                    "__typename": "ProjectV2ItemFieldSingleSelectValue",
                                    "name": "In Progress",
                                    "field": {"name": "Status"},
                                },
                            ]
                        },
                    },
                    {
                        "id": "PVTI_2",
                        "type": "PULL_REQUEST",
                        "content": {
                            "__typename": "PullRequest",
                            "title": "Add feature",
                            "number": 99,
                        },
                        "fieldValues": {"nodes": []},
                    },
                    {
                        "id": "PVTI_3",
                        "type": "DRAFT_ISSUE",
                        "content": {
                            "__typename": "DraftIssue",
                            "title": "Brainstorm",
                        },
                        "fieldValues": {
                            "nodes": [
                                {
                                    "__typename": "ProjectV2ItemFieldTextValue",
                                    "text": "Brainstorm ideas",
                                    "field": {"name": "Title"},
                                },
                            ]
                        },
                    },
                ]
            }
        }
    }
}


@patch("cai.github.projects.requests.post")
def test_get_project_items_issue(mock_post, bot):
    """Parse an Issue content type correctly."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = _ITEMS_RESPONSE
    mock_post.return_value = mock_resp

    result = get_project_items(bot, "o/r", "PVT_1")

    assert len(result) == 3

    issue = result[0]
    assert issue.id == "PVTI_1"
    assert issue.content_type == "Issue"
    assert issue.content_title == "Fix bug"
    assert issue.content_number == 42
    assert issue.field_values == {"Title": "Fix login bug", "Status": "In Progress"}


@patch("cai.github.projects.requests.post")
def test_get_project_items_pr(mock_post, bot):
    """Parse a PullRequest content type correctly."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = _ITEMS_RESPONSE
    mock_post.return_value = mock_resp

    result = get_project_items(bot, "o/r", "PVT_1")

    pr_item = result[1]
    assert pr_item.id == "PVTI_2"
    assert pr_item.content_type == "PullRequest"
    assert pr_item.content_title == "Add feature"
    assert pr_item.content_number == 99
    assert pr_item.field_values == {}


@patch("cai.github.projects.requests.post")
def test_get_project_items_draft(mock_post, bot):
    """Parse a DraftIssue content type correctly (no number)."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = _ITEMS_RESPONSE
    mock_post.return_value = mock_resp

    result = get_project_items(bot, "o/r", "PVT_1")

    draft = result[2]
    assert draft.id == "PVTI_3"
    assert draft.content_type == "DraftIssue"
    assert draft.content_title == "Brainstorm"
    assert draft.content_number is None
    assert draft.field_values == {"Title": "Brainstorm ideas"}


@patch("cai.github.projects.requests.post")
def test_get_project_items_null_content(mock_post, bot):
    """Item with no content (e.g. converted from note)."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "data": {
            "node": {
                "items": {
                    "nodes": [
                        {
                            "id": "PVTI_X",
                            "type": "ISSUE",
                            "content": None,
                            "fieldValues": {"nodes": []},
                        }
                    ]
                }
            }
        }
    }
    mock_post.return_value = mock_resp

    result = get_project_items(bot, "o/r", "PVT_1")

    assert len(result) == 1
    assert result[0].content_type == "ISSUE"
    assert result[0].content_title == ""
    assert result[0].content_number is None


@patch("cai.github.projects.requests.post")
def test_get_project_items_empty(mock_post, bot):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "data": {"node": {"items": {"nodes": []}}}
    }
    mock_post.return_value = mock_resp

    result = get_project_items(bot, "o/r", "PVT_1")

    assert result == []


# ---------------------------------------------------------------------------
# _parse_field_values
# ---------------------------------------------------------------------------


def test_parse_field_values_empty_list():
    """An empty list produces an empty dict."""
    result = _parse_field_values([])
    assert result == {}


def test_parse_field_values_text_and_single_select():
    nodes = [
        {
            "__typename": "ProjectV2ItemFieldTextValue",
            "text": "Hello world",
            "field": {"name": "Title"},
        },
        {
            "__typename": "ProjectV2ItemFieldSingleSelectValue",
            "name": "Done",
            "field": {"name": "Status"},
        },
    ]
    result = _parse_field_values(nodes)
    assert result == {"Title": "Hello world", "Status": "Done"}


def test_parse_field_values_null_text():
    nodes = [
        {
            "__typename": "ProjectV2ItemFieldTextValue",
            "text": None,
            "field": {"name": "Notes"},
        },
    ]
    result = _parse_field_values(nodes)
    assert result == {"Notes": ""}


def test_parse_field_values_null_name():
    nodes = [
        {
            "__typename": "ProjectV2ItemFieldSingleSelectValue",
            "name": None,
            "field": {"name": "Status"},
        },
    ]
    result = _parse_field_values(nodes)
    assert result == {"Status": ""}


def test_parse_field_values_missing_field():
    nodes = [
        {
            "__typename": "ProjectV2ItemFieldTextValue",
            "text": "orphan",
        },
    ]
    result = _parse_field_values(nodes)
    assert result == {}


def test_parse_field_values_missing_field_name():
    nodes = [
        {
            "__typename": "ProjectV2ItemFieldTextValue",
            "text": "value",
            "field": {},
        },
    ]
    result = _parse_field_values(nodes)
    assert result == {}


def test_parse_field_values_unknown_typename_skipped():
    nodes = [
        {
            "__typename": "ProjectV2ItemFieldNumberValue",
            "number": 42,
            "field": {"name": "Points"},
        },
        {
            "__typename": "ProjectV2ItemFieldTextValue",
            "text": "Keep",
            "field": {"name": "Title"},
        },
    ]
    result = _parse_field_values(nodes)
    assert result == {"Title": "Keep"}


# ---------------------------------------------------------------------------
# add_item_to_project
# ---------------------------------------------------------------------------


@patch("cai.github.projects.requests.post")
def test_add_item_to_project_returns_item_id(mock_post, bot):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "data": {
            "addProjectV2ItemById": {
                "item": {"id": "PVTI_new"}
            }
        }
    }
    mock_post.return_value = mock_resp

    result = add_item_to_project(bot, "o/r", "PVT_1", "I_42")

    assert result == "PVTI_new"
    call_args, call_kwargs = mock_post.call_args
    assert call_kwargs["json"]["variables"] == {
        "projectId": "PVT_1",
        "contentId": "I_42",
    }


@patch("cai.github.projects.requests.post")
def test_add_item_to_project_verifies_mutation(mock_post, bot):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "data": {
            "addProjectV2ItemById": {
                "item": {"id": "PVTI_x"}
            }
        }
    }
    mock_post.return_value = mock_resp

    add_item_to_project(bot, "o/r", "PVT_1", "I_1")

    call_args, call_kwargs = mock_post.call_args
    assert "addProjectV2ItemById" in call_kwargs["json"]["query"]


@patch("cai.github.projects.requests.post")
def test_graphql_passes_timeout(mock_post, bot):
    """_graphql passes timeout=30 to requests.post."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"data": {}}
    mock_post.return_value = mock_resp

    _graphql(bot, "o/r", "q", {})

    assert mock_post.call_args[1]["timeout"] == 30


# ---------------------------------------------------------------------------
# set_item_field_value
# ---------------------------------------------------------------------------


@patch("cai.github.projects.requests.post")
def test_set_item_field_value_text(mock_post, bot):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"data": {}}
    mock_post.return_value = mock_resp

    set_item_field_value(
        bot, "o/r", "PVT_1", "PVTI_1", "PF_Title", "New title"
    )

    call_args, call_kwargs = mock_post.call_args
    variables = call_kwargs["json"]["variables"]
    assert variables["projectId"] == "PVT_1"
    assert variables["itemId"] == "PVTI_1"
    assert variables["fieldId"] == "PF_Title"
    assert variables["value"] == {"text": "New title"}
    assert "updateProjectV2ItemFieldValue" in call_kwargs["json"]["query"]


@patch("cai.github.projects.requests.post")
def test_set_item_field_value_single_select(mock_post, bot):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"data": {}}
    mock_post.return_value = mock_resp

    set_item_field_value(
        bot,
        "o/r",
        "PVT_1",
        "PVTI_1",
        "PF_Status",
        "opt_abc",
        value_type="single_select",
    )

    call_args, call_kwargs = mock_post.call_args
    variables = call_kwargs["json"]["variables"]
    assert variables["value"] == {"singleSelectOptionId": "opt_abc"}


@patch("cai.github.projects.requests.post")
def test_set_item_field_value_default_value_type_is_text(mock_post, bot):
    """When value_type is not passed, it defaults to 'text'."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"data": {}}
    mock_post.return_value = mock_resp

    set_item_field_value(bot, "o/r", "PVT_1", "PVTI_1", "PF_X", "hello")

    call_args, call_kwargs = mock_post.call_args
    assert call_kwargs["json"]["variables"]["value"] == {"text": "hello"}


@patch("cai.github.projects.requests.post")
def test_set_item_field_value_unknown_value_type_falls_to_text(mock_post, bot):
    """An unknown value_type gracefully falls back to the 'text' packaging."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"data": {}}
    mock_post.return_value = mock_resp

    set_item_field_value(
        bot, "o/r", "PVT_1", "PVTI_1", "PF_X", "some-val",
        value_type="date",  # not a recognised type → else branch
    )

    call_args, call_kwargs = mock_post.call_args
    assert call_kwargs["json"]["variables"]["value"] == {"text": "some-val"}
