"""Tests for ``cai.workflows.parent_check``.

Covers the minimal placeholder stub — graph construction, node execution,
and the CLI entry point.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from pydantic_graph import End, Graph, GraphRunContext

from cai.workflows.parent_check import _PlaceholderNode, _PlaceholderState, main, parent_check_graph


class TestPlaceholderNode:
    """The ``_PlaceholderNode`` is the only node in ``parent_check_graph``."""

    def test_run_returns_end(self):
        """``run()`` returns ``End(None)`` for any state."""
        node = _PlaceholderNode()
        ctx = GraphRunContext(state=_PlaceholderState(), deps=None)
        result = asyncio.run(node.run(ctx))
        assert isinstance(result, End)
        assert result.data is None


class TestParentCheckGraph:
    """The ``parent_check_graph`` is a ``pydantic_graph.Graph``."""

    def test_is_pydantic_graph(self):
        assert isinstance(parent_check_graph, Graph)

    def test_contains_placeholder_node(self):
        assert _PlaceholderNode in parent_check_graph.get_nodes()

    def test_graph_run_completes(self):
        """A full graph run from the placeholder node returns End."""
        result = asyncio.run(
            parent_check_graph.run(
                _PlaceholderNode(),
                state=_PlaceholderState(),
            )
        )
        assert result.output is None


class TestMain:
    """The CLI entry point runs the graph without error."""

    def test_main_runs_without_error(self):
        """``main()`` runs the graph to completion."""
        with patch.object(asyncio, "run") as mock_run:
            main()
            mock_run.assert_called_once()
