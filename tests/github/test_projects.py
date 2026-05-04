"""Tests for cai.github.projects — GitHub Projects V2 GraphQL helpers."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from cai.github.projects import (
    _owner_name,
    add_item_to_project,
    get_issue_node_id,
    get_project_id,
    set_status,
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
# _owner_name (internal helper)
# ---------------------------------------------------------------------------


def test_owner_name_simple():
    """Splits 'owner/repo' into (owner, name)."""
    assert _owner_name("acme/widgets") == ("acme", "widgets")


def test_owner_name_multi_segment():
    """Splits 'org/team/repo' — only first segment before '/' is owner."""
    assert _owner_name("my-org/team/repo") == ("my-org", "team/repo")


def test_owner_name_no_slash():
    """When there's no '/', owner is the whole string and name is empty."""
    assert _owner_name("bare-repo") == ("bare-repo", "")


def test_owner_name_empty():
    """Empty string yields ('', '')."""
    assert _owner_name("") == ("", "")


# ---------------------------------------------------------------------------
# get_issue_node_id
# ---------------------------------------------------------------------------


@patch("cai.github.projects._graphql")
def test_get_issue_node_id(mock_graphql, bot):
    mock_graphql.return_value = {
        "repository": {"issue": {"id": "I_kwDOBm31kM5xK8w-"}}
    }

    result = get_issue_node_id(bot, "acme/widgets", 42)

    assert result == "I_kwDOBm31kM5xK8w-"
    mock_graphql.assert_called_once()
    _, _, variables = mock_graphql.call_args[0]
    assert variables["owner"] == "acme"
    assert variables["name"] == "widgets"
    assert variables["number"] == 42


@patch("cai.github.projects._graphql")
def test_get_issue_node_id_not_found(mock_graphql, bot):
    """When issue is null, ValueError is raised with issue number and repo."""
    mock_graphql.return_value = {
        "repository": {"issue": None}
    }

    with pytest.raises(ValueError, match="Issue #99999 not found in acme/widgets"):
        get_issue_node_id(bot, "acme/widgets", 99999)


# ---------------------------------------------------------------------------
# get_project_id
# ---------------------------------------------------------------------------


@patch("cai.github.projects._graphql")
def test_get_project_id(mock_graphql, bot):
    mock_graphql.return_value = {
        "organization": {"projectV2": {"id": "PVT_kwDOBm31kM4B"}}
    }

    result = get_project_id(bot, "acme/widgets", 1)

    assert result == "PVT_kwDOBm31kM4B"
    mock_graphql.assert_called_once()
    _, _, variables = mock_graphql.call_args[0]
    assert variables["owner"] == "acme"
    assert variables["name"] == "widgets"
    assert variables["number"] == 1


@patch("cai.github.projects._graphql")
def test_get_project_id_not_found(mock_graphql, bot):
    mock_graphql.return_value = {
        "organization": {"projectV2": None}
    }

    with pytest.raises(ValueError, match="Project #99 not found in org acme"):
        get_project_id(bot, "acme/widgets", 99)


# ---------------------------------------------------------------------------
# add_item_to_project
# ---------------------------------------------------------------------------


@patch("cai.github.projects._graphql")
def test_add_item_to_project(mock_graphql, bot):
    mock_graphql.return_value = {
        "addProjectV2ItemById": {"item": {"id": "PVTI_lADOBm31kM4BgM0B"}}
    }

    result = add_item_to_project(
        bot, "acme/widgets",
        project_id="PVT_kwDOBm31kM4B",
        content_id="I_kwDOBm31kM5xK8w-",
    )

    assert result == "PVTI_lADOBm31kM4BgM0B"
    mock_graphql.assert_called_once()
    _, _, variables = mock_graphql.call_args[0]
    assert variables["projectId"] == "PVT_kwDOBm31kM4B"
    assert variables["contentId"] == "I_kwDOBm31kM5xK8w-"
    assert variables["owner"] == "acme"
    assert variables["name"] == "widgets"


@patch("cai.github.projects._graphql")
def test_add_item_to_project_graphql_error(mock_graphql, bot):
    """GraphQL error propagates from add_item_to_project."""
    mock_graphql.side_effect = RuntimeError("GraphQL error: [{'message': 'not found'}]")

    with pytest.raises(RuntimeError, match="GraphQL error"):
        add_item_to_project(
            bot, "acme/widgets",
            project_id="PVT_kwDOBm31kM4B",
            content_id="I_kwDOBm31kM5xK8w-",
        )


@patch("cai.github.projects._graphql")
def test_add_item_to_project_http_error(mock_graphql, bot):
    """HTTP error propagates from add_item_to_project."""
    mock_graphql.side_effect = requests.HTTPError("401 Unauthorized")

    with pytest.raises(requests.HTTPError, match="401 Unauthorized"):
        add_item_to_project(
            bot, "acme/widgets",
            project_id="PVT_kwDOBm31kM4B",
            content_id="I_kwDOBm31kM5xK8w-",
        )


# ---------------------------------------------------------------------------
# set_status
# ---------------------------------------------------------------------------


@patch("cai.github.projects._graphql")
def test_set_status(mock_graphql, bot):
    """Happy path: Status field exists with the requested option."""
    field_query_response = {
        "node": {
            "fields": {
                "nodes": [
                    {
                        "id": "PVTF_lADO...",
                        "name": "Status",
                        "options": [
                            {"id": "opt-1", "name": "Todo"},
                            {"id": "opt-2", "name": "In Progress"},
                            {"id": "opt-3", "name": "Done"},
                        ],
                    }
                ]
            }
        }
    }
    mutation_response = {
        "updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "PVTI_..."}}
    }
    mock_graphql.side_effect = [field_query_response, mutation_response]

    set_status(
        bot, "acme/widgets",
        project_id="PVT_kwDOBm31kM4B",
        item_id="PVTI_lADOBm31kM4BgM0B",
        status="In Progress",
    )

    assert mock_graphql.call_count == 2

    # First call — field query
    _, query1, variables1 = mock_graphql.call_args_list[0][0]
    assert "node(id:" in query1
    assert variables1["projectId"] == "PVT_kwDOBm31kM4B"
    assert variables1["owner"] == "acme"
    assert variables1["name"] == "widgets"

    # Second call — mutation
    _, query2, variables2 = mock_graphql.call_args_list[1][0]
    assert "updateProjectV2ItemFieldValue" in query2
    assert variables2["projectId"] == "PVT_kwDOBm31kM4B"
    assert variables2["itemId"] == "PVTI_lADOBm31kM4BgM0B"
    assert variables2["fieldId"] == "PVTF_lADO..."
    assert variables2["value"] == {"singleSelectOptionId": "opt-2"}
    assert variables2["owner"] == "acme"
    assert variables2["name"] == "widgets"


@patch("cai.github.projects._graphql")
def test_set_status_no_status_field(mock_graphql, bot):
    """Project has fields but none named 'Status'."""
    mock_graphql.return_value = {
        "node": {
            "fields": {
                "nodes": [
                    {
                        "id": "PVTF_OTHER",
                        "name": "Priority",
                        "options": [
                            {"id": "p1", "name": "High"},
                            {"id": "p2", "name": "Low"},
                        ],
                    }
                ]
            }
        }
    }

    with pytest.raises(ValueError, match="No 'Status' field found"):
        set_status(
            bot, "acme/widgets",
            project_id="PVT_kwDOBm31kM4B",
            item_id="PVTI_...",
            status="Done",
        )

    mock_graphql.assert_called_once()


@patch("cai.github.projects._graphql")
def test_set_status_unknown_option(mock_graphql, bot):
    """Status field exists but doesn't include the requested option name."""
    mock_graphql.return_value = {
        "node": {
            "fields": {
                "nodes": [
                    {
                        "id": "PVTF_STATUS",
                        "name": "Status",
                        "options": [
                            {"id": "opt-1", "name": "Todo"},
                            {"id": "opt-3", "name": "Done"},
                        ],
                    }
                ]
            }
        }
    }

    with pytest.raises(ValueError, match="Status 'In Progress' not found"):
        set_status(
            bot, "acme/widgets",
            project_id="PVT_kwDOBm31kM4B",
            item_id="PVTI_...",
            status="In Progress",
        )

    mock_graphql.assert_called_once()


# ---------------------------------------------------------------------------
# GraphQL error propagation
# ---------------------------------------------------------------------------


@patch("cai.github.projects._graphql")
def test_graphql_errors_propagated_from_get_project_id(mock_graphql, bot):
    """_graphql's RuntimeError on 'errors' payload propagates through new functions."""
    mock_graphql.side_effect = RuntimeError("GraphQL error: [{'message': 'bad query'}]")

    with pytest.raises(RuntimeError, match="GraphQL error"):
        get_project_id(bot, "acme/widgets", 1)


@patch("cai.github.projects._graphql")
def test_graphql_http_error_propagated(mock_graphql, bot):
    """_graphql's HTTPError propagates through new functions."""
    mock_graphql.side_effect = requests.HTTPError("500 Server Error")

    with pytest.raises(requests.HTTPError, match="500 Server Error"):
        get_issue_node_id(bot, "acme/widgets", 1)


# ---------------------------------------------------------------------------
# set_status — additional edge cases
# ---------------------------------------------------------------------------


@patch("cai.github.projects._graphql")
def test_set_status_empty_fields(mock_graphql, bot):
    """Project has zero custom fields — no Status field found."""
    mock_graphql.return_value = {
        "node": {
            "fields": {
                "nodes": []
            }
        }
    }

    with pytest.raises(ValueError, match="No 'Status' field found"):
        set_status(
            bot, "acme/widgets",
            project_id="PVT_kwDOBm31kM4B",
            item_id="PVTI_...",
            status="Todo",
        )

    mock_graphql.assert_called_once()


@patch("cai.github.projects._graphql")
def test_set_status_graphql_error(mock_graphql, bot):
    """GraphQL error on the field-query call propagates through set_status."""
    mock_graphql.side_effect = RuntimeError("GraphQL error: [{'message': 'timeout'}]")

    with pytest.raises(RuntimeError, match="GraphQL error"):
        set_status(
            bot, "acme/widgets",
            project_id="PVT_kwDOBm31kM4B",
            item_id="PVTI_...",
            status="Todo",
        )


@patch("cai.github.projects._graphql")
def test_set_status_http_error(mock_graphql, bot):
    """HTTP error on the field-query call propagates through set_status."""
    mock_graphql.side_effect = requests.HTTPError("403 Forbidden")

    with pytest.raises(requests.HTTPError, match="403 Forbidden"):
        set_status(
            bot, "acme/widgets",
            project_id="PVT_kwDOBm31kM4B",
            item_id="PVTI_...",
            status="Todo",
        )


@patch("cai.github.projects._graphql")
def test_set_status_mutation_graphql_error(mock_graphql, bot):
    """GraphQL error on the *mutation* call propagates through set_status."""
    field_query_response = {
        "node": {
            "fields": {
                "nodes": [
                    {
                        "id": "PVTF_STATUS",
                        "name": "Status",
                        "options": [
                            {"id": "opt-1", "name": "Todo"},
                            {"id": "opt-2", "name": "Done"},
                        ],
                    }
                ]
            }
        }
    }
    mock_graphql.side_effect = [
        field_query_response,
        RuntimeError("GraphQL error: [{'message': 'mutation failed'}]"),
    ]

    with pytest.raises(RuntimeError, match="GraphQL error"):
        set_status(
            bot, "acme/widgets",
            project_id="PVT_kwDOBm31kM4B",
            item_id="PVTI_...",
            status="Todo",
        )

    assert mock_graphql.call_count == 2
