import os
import pytest
from unittest.mock import MagicMock, patch

from cai.log.observability import (
    session_id_for_pr,
    setup_langfuse,
    langfuse_workflow,
)


def test_session_id_for_pr_cai_branch():
    assert session_id_for_pr(123, "cai/solve-456") == "issue-456"

def test_session_id_for_pr_other_branch():
    assert session_id_for_pr(123, "feature/my-branch") == "pr-123"

def test_session_id_for_pr_no_branch():
    assert session_id_for_pr(123, None) == "pr-123"

@patch("cai.log.observability.setup_langfuse", return_value=True)
@patch("cai.log.observability.get_client")
@patch("cai.log.observability.propagate_attributes")
def test_langfuse_workflow_enabled(
    mock_propagate,
    mock_get_client,
    mock_setup_langfuse,
):
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client
    
    mock_observation_context = MagicMock()
    mock_client.start_as_current_observation.return_value = mock_observation_context

    mock_propagate_context = MagicMock()
    mock_propagate.return_value = mock_propagate_context

    # Simulate entering context managers
    mock_observation_context.__enter__ = MagicMock()
    mock_observation_context.__exit__ = MagicMock()
    mock_propagate_context.__enter__ = MagicMock()
    mock_propagate_context.__exit__ = MagicMock()

    with langfuse_workflow(
        name="test-workflow",
        input={"test": "input"},
        metadata={"test": "meta"},
        session_id="test-session",
    ):
        pass

    mock_setup_langfuse.assert_called_once()
    mock_client.start_as_current_observation.assert_called_once_with(
        name="test-workflow",
        as_type="agent",
        input={"test": "input"},
        metadata={"test": "meta"},
    )
    mock_propagate.assert_called_once_with(session_id="test-session")
    
    # Check that context managers were actually entered
    mock_observation_context.__enter__.assert_called_once()
    mock_observation_context.__exit__.assert_called_once()
    mock_propagate_context.__enter__.assert_called_once()
    mock_propagate_context.__exit__.assert_called_once()

@patch("cai.log.observability.setup_langfuse", return_value=True)
@patch("cai.log.observability.get_client")
@patch("cai.log.observability.propagate_attributes")
def test_langfuse_workflow_no_session_id(
    mock_propagate,
    mock_get_client,
    mock_setup_langfuse,
):
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client
    
    mock_observation_context = MagicMock()
    mock_client.start_as_current_observation.return_value = mock_observation_context

    # Simulate entering context managers
    mock_observation_context.__enter__ = MagicMock()
    mock_observation_context.__exit__ = MagicMock()

    with langfuse_workflow(name="test-workflow"):
        pass

    mock_client.start_as_current_observation.assert_called_once_with(
        name="test-workflow",
        as_type="agent",
        input=None,
        metadata=None,
    )
    mock_propagate.assert_not_called()

@patch("cai.log.observability.setup_langfuse", return_value=False)
@patch("cai.log.observability.get_client")
def test_langfuse_workflow_disabled(mock_get_client, mock_setup_langfuse):
    with langfuse_workflow("test-workflow"):
        pass

    mock_setup_langfuse.assert_called_once()
    mock_get_client.assert_not_called()
