"""Tests for cai.github.projects — project items, fields, and GraphQL helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from cai.github.projects import (
    _graphql,
    add_item_to_project,
    get_issue_node_id,
    get_project_fields,
    get_project_id,
    set_single_select_field,
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
# _graphql
# ---------------------------------------------------------------------------


@patch("cai.github.projects.requests.post")
def test_graphql_uses_explicit_repo_for_token(mock_post, bot):
    """_graphql calls token_for with the explicit ``repo`` argument."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"data": {"some": "payload"}}
    mock_post.return_value = mock_resp

    result = _graphql(bot, "query { foo }", {"projectId": "PVT_1"}, "acme/widgets")

    assert result == {"some": "payload"}
    bot.token_for.assert_called_once_with("acme/widgets")


@patch("cai.github.projects.requests.post")
def test_graphql_passes_correct_headers(mock_post, bot):
    """_graphql sets Authorization, Accept, and X-GitHub-Api-Version headers."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"data": {}}
    mock_post.return_value = mock_resp

    _graphql(bot, "q", {}, "x/y")

    headers = mock_post.call_args[1]["headers"]
    assert headers["Authorization"] == "Bearer fake-token"
    assert headers["Accept"] == "application/vnd.github+json"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"


@patch("cai.github.projects.requests.post")
def test_graphql_posts_to_graphql_url(mock_post, bot):
    """_graphql POSTs to the GitHub GraphQL endpoint."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"data": {}}
    mock_post.return_value = mock_resp

    _graphql(bot, "query { x }", {"a": 1}, "o/r")

    mock_post.assert_called_once()
    assert mock_post.call_args[0][0] == "https://api.github.com/graphql"
    assert mock_post.call_args[1]["json"]["query"] == "query { x }"
    assert mock_post.call_args[1]["json"]["variables"] == {"a": 1}


@patch("cai.github.projects.requests.post")
def test_graphql_raises_on_http_error(mock_post, bot):
    """_graphql raises HTTPError via raise_for_status()."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = requests.HTTPError("500 Server Error")
    mock_post.return_value = mock_resp

    with pytest.raises(requests.HTTPError, match="500 Server Error"):
        _graphql(bot, "q", {}, "x/y")


@patch("cai.github.projects.requests.post")
def test_graphql_raises_on_errors_key(mock_post, bot):
    """_graphql raises RuntimeError when the response contains 'errors'."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {"errors": [{"message": "bad query"}]}
    mock_post.return_value = mock_resp

    with pytest.raises(RuntimeError, match="GraphQL error"):
        _graphql(bot, "q", {}, "x/y")


@patch("cai.github.projects.requests.post")
def test_graphql_returns_data(mock_post, bot):
    """_graphql returns the 'data' key from the response payload."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"data": {"node": {"id": "abc"}}}
    mock_post.return_value = mock_resp

    result = _graphql(bot, "query { node { id } }", {}, "x/y")
    assert result == {"node": {"id": "abc"}}


# ---------------------------------------------------------------------------
# get_project_id
# ---------------------------------------------------------------------------


@patch("cai.github.projects._graphql")
def test_get_project_id_org(mock_graphql, bot):
    """Returns the project id from the organization resolver."""
    mock_graphql.return_value = {
        "organization": {"projectV2": {"id": "PVT_org1"}},
        "user": {"projectV2": None},
    }

    result = get_project_id(bot, "acme/repo", "acme", 1)

    assert result == "PVT_org1"
    mock_graphql.assert_called_once_with(
        bot,
        mock_graphql.call_args[0][1],  # query
        {"owner": "acme", "number": 1},
        "acme/repo",
    )


@patch("cai.github.projects._graphql")
def test_get_project_id_user(mock_graphql, bot):
    """Returns the project id from the user resolver."""
    mock_graphql.return_value = {
        "organization": {"projectV2": None},
        "user": {"projectV2": {"id": "PVT_user1"}},
    }

    result = get_project_id(bot, "acme/repo", "jdoe", 2)

    assert result == "PVT_user1"


@patch("cai.github.projects._graphql")
def test_get_project_id_both_none_raises(mock_graphql, bot):
    """Raises ValueError when neither org nor user has the project."""
    mock_graphql.return_value = {
        "organization": {"projectV2": None},
        "user": {"projectV2": None},
    }

    with pytest.raises(ValueError, match="not found"):
        get_project_id(bot, "acme/repo", "acme", 99)


@patch("cai.github.projects._graphql")
def test_get_project_id_missing_keys(mock_graphql, bot):
    """Handles response where org/user keys are absent entirely."""
    mock_graphql.return_value = {}

    with pytest.raises(ValueError, match="not found"):
        get_project_id(bot, "acme/repo", "acme", 99)


# ---------------------------------------------------------------------------
# get_issue_node_id
# ---------------------------------------------------------------------------


@patch("cai.github.projects._graphql")
def test_get_issue_node_id(mock_graphql, bot):
    """Returns the GraphQL node ID for an issue."""
    mock_graphql.return_value = {
        "repository": {"issue": {"id": "I_kw123"}},
    }

    result = get_issue_node_id(bot, "acme/widgets", 42)

    assert result == "I_kw123"
    mock_graphql.assert_called_once_with(
        bot,
        mock_graphql.call_args[0][1],
        {"owner": "acme", "name": "widgets", "number": 42},
        "acme/widgets",
    )


@patch("cai.github.projects._graphql")
def test_get_issue_node_id_splits_repo_correctly(mock_graphql, bot):
    """Splits 'org/team/repo' on first slash only."""
    mock_graphql.return_value = {
        "repository": {"issue": {"id": "I_xyz"}},
    }

    result = get_issue_node_id(bot, "my-org/my-team/repo", 7)
    assert result == "I_xyz"
    _, _, variables, _ = mock_graphql.call_args[0]
    assert variables["owner"] == "my-org"
    assert variables["name"] == "my-team/repo"


@patch("cai.github.projects._graphql")
def test_get_issue_node_id_not_found(mock_graphql, bot):
    """Raises ValueError when the issue is None."""
    mock_graphql.return_value = {
        "repository": {"issue": None},
    }

    with pytest.raises(ValueError, match="not found"):
        get_issue_node_id(bot, "acme/widgets", 999)


@patch("cai.github.projects._graphql")
def test_get_issue_node_id_missing_keys(mock_graphql, bot):
    """Handles response where repository/issue keys are absent."""
    mock_graphql.return_value = {}

    with pytest.raises(ValueError, match="not found"):
        get_issue_node_id(bot, "acme/widgets", 1)


# ---------------------------------------------------------------------------
# add_item_to_project
# ---------------------------------------------------------------------------


@patch("cai.github.projects._graphql")
def test_add_item_to_project(mock_graphql, bot):
    """Returns the project-item node ID from the mutation response."""
    mock_graphql.return_value = {
        "addProjectV2ItemById": {"item": {"id": "PVTI_abc"}},
    }

    result = add_item_to_project(bot, "acme/repo", "PVT_1", "I_kw123")

    assert result == "PVTI_abc"
    mock_graphql.assert_called_once_with(
        bot,
        mock_graphql.call_args[0][1],
        {"projectId": "PVT_1", "contentId": "I_kw123"},
        "acme/repo",
    )


# ---------------------------------------------------------------------------
# get_project_fields
# ---------------------------------------------------------------------------


@patch("cai.github.projects._graphql")
def test_get_project_fields_plain_and_single_select(mock_graphql, bot):
    """Returns a dict keyed by field name with id and optional options."""
    mock_graphql.return_value = {
        "node": {
            "fields": {
                "nodes": [
                    {"id": "F1", "name": "Title"},
                    {"id": "F2", "name": "Status", "options": [
                        {"id": "opt1", "name": "Todo"},
                        {"id": "opt2", "name": "Done"},
                    ]},
                ]
            }
        }
    }

    result = get_project_fields(bot, "acme/repo", "PVT_1")

    assert result == {
        "Title": {"id": "F1"},
        "Status": {"id": "F2", "options": {"Todo": "opt1", "Done": "opt2"}},
    }
    mock_graphql.assert_called_once_with(
        bot,
        mock_graphql.call_args[0][1],
        {"projectId": "PVT_1"},
        "acme/repo",
    )


@patch("cai.github.projects._graphql")
def test_get_project_fields_empty(mock_graphql, bot):
    """Returns an empty dict when there are no fields."""
    mock_graphql.return_value = {"node": {"fields": {"nodes": []}}}

    result = get_project_fields(bot, "acme/repo", "PVT_1")
    assert result == {}


@patch("cai.github.projects._graphql")
def test_get_project_fields_only_plain_fields(mock_graphql, bot):
    """Fields without options get only the 'id' key."""
    mock_graphql.return_value = {
        "node": {
            "fields": {
                "nodes": [
                    {"id": "F1", "name": "Title"},
                    {"id": "F2", "name": "Assignees"},
                ]
            }
        }
    }

    result = get_project_fields(bot, "acme/repo", "PVT_1")
    assert result == {"Title": {"id": "F1"}, "Assignees": {"id": "F2"}}


# ---------------------------------------------------------------------------
# set_single_select_field
# ---------------------------------------------------------------------------


@patch("cai.github.projects._graphql")
def test_set_single_select_field(mock_graphql, bot):
    """Calls _graphql with the mutation and singleSelectOptionId value."""
    set_single_select_field(bot, "acme/repo", "PVT_1", "PVTI_abc", "F_status", "opt_done")

    mock_graphql.assert_called_once()
    _, _, variables, _ = mock_graphql.call_args[0]
    assert variables["projectId"] == "PVT_1"
    assert variables["itemId"] == "PVTI_abc"
    assert variables["fieldId"] == "F_status"
    assert variables["value"] == {"singleSelectOptionId": "opt_done"}
