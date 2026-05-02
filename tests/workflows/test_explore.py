from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from pydantic_ai.usage import UsageLimits

from cai.workflows.explore import ExploreNode
from cai.workflows.refine import RefineNode


def _run(node, state):
    ctx = MagicMock()
    ctx.state = state
    return asyncio.run(node.run(ctx))


# ---------------------------------------------------------------------------
# ExploreNode — request limit
# ---------------------------------------------------------------------------


@patch("cai.workflows.explore._explore_agent")
def test_explore_node_request_limit(mock_agent_factory, state):
    """ExploreNode passes UsageLimits with request_limit=100 to the explore agent."""
    agent_instance = MagicMock()
    mock_agent_factory.return_value = agent_instance

    async def mock_run(prompt, *args, **kwargs):
        result = MagicMock()
        result.output = MagicMock()
        return result

    agent_instance.run = MagicMock(side_effect=mock_run)

    result = _run(ExploreNode(), state)

    assert isinstance(result, RefineNode)
    agent_instance.run.assert_called_once()

    _, kwargs = agent_instance.run.call_args
    assert "usage_limits" in kwargs
    assert isinstance(kwargs["usage_limits"], UsageLimits)
    assert kwargs["usage_limits"].request_limit == 100
