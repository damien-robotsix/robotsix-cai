"""Tests for ``cai.workflows.parent_check``.

Covers the full parent-check workflow: FetchParentNode (no parent,
siblings open, all closed), VerifyParentNode (fulfilled closes parent,
incomplete creates sub-issues), and the CLI entry point.
"""
from __future__ import annotations

import asyncio
import io
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pydantic_graph import End, GraphRunContext

from cai.workflows.parent_check import (
    FetchParentNode,
    ParentCheckOutput,
    ParentCheckState,
    VerifyParentNode,
    main,
    parent_check_graph,
)


def _state(**kwargs) -> ParentCheckState:
    defaults = {
        "bot": MagicMock(),
        "repo": "owner/repo",
        "sub_issue_number": 99,
    }
    defaults.update(kwargs)
    return ParentCheckState(**defaults)


# ---------------------------------------------------------------------------
# ParentCheckOutput model
# ---------------------------------------------------------------------------


class TestParentCheckOutput:
    def test_model_json_schema_has_no_refs(self):
        """``model_json_schema`` resolves ``$ref`` pointers."""
        schema = ParentCheckOutput.model_json_schema()
        assert "$ref" not in json.dumps(schema)
        # Core fields are present
        props = schema.get("properties", {})
        assert "all_fulfilled" in props
        assert "reason" in props
        assert "new_sub_issues" in props

    def test_default_new_sub_issues_is_empty(self):
        """``new_sub_issues`` defaults to an empty list."""
        out = ParentCheckOutput(all_fulfilled=True, reason="Done.")
        assert out.new_sub_issues == []


# ---------------------------------------------------------------------------
# parent_verifier_agent helper
# ---------------------------------------------------------------------------


class TestParentVerifierAgent:
    def test_lru_cache_single_instance(self):
        """``parent_verifier_agent`` caches its result."""
        from cai.workflows.parent_check import parent_verifier_agent

        parent_verifier_agent.cache_clear()
        try:
            with patch(
                "cai.workflows.parent_check.build_deep_agent"
            ) as mock_build:
                mock_build.return_value = MagicMock()
                agent1 = parent_verifier_agent()
                agent2 = parent_verifier_agent()
                assert agent1 is agent2
                mock_build.assert_called_once()
        finally:
            parent_verifier_agent.cache_clear()

    def test_cache_cleared_on_clear(self):
        """Clearing the cache returns a fresh instance."""
        from cai.workflows.parent_check import parent_verifier_agent

        parent_verifier_agent.cache_clear()
        try:
            with patch(
                "cai.workflows.parent_check.build_deep_agent"
            ) as mock_build:
                mock_build.side_effect = lambda *a, **kw: MagicMock()
                agent1 = parent_verifier_agent()
                parent_verifier_agent.cache_clear()
                agent2 = parent_verifier_agent()
                assert agent1 is not agent2
                assert mock_build.call_count == 2
        finally:
            parent_verifier_agent.cache_clear()


# ---------------------------------------------------------------------------
# ParentCheckState dataclass
# ---------------------------------------------------------------------------


class TestParentCheckState:
    def test_default_parent_number_is_none(self):
        """``parent_number`` defaults to ``None``."""
        state = _state()
        assert state.parent_number is None

    def test_default_parent_body_is_empty(self):
        """``parent_body`` defaults to ``""``."""
        state = _state()
        assert state.parent_body == ""

    def test_default_sub_issues_summary_is_empty(self):
        """``sub_issues_summary`` defaults to ``""``."""
        state = _state()
        assert state.sub_issues_summary == ""

    def test_default_output_is_none(self):
        """``output`` defaults to ``None``."""
        state = _state()
        assert state.output is None


# ---------------------------------------------------------------------------
# _scratch_deps helper
# ---------------------------------------------------------------------------


class TestScratchDeps:
    def test_returns_deep_agent_deps_with_temp_dir(self):
        """``_scratch_deps`` creates a temp dir and returns ``DeepAgentDeps``."""
        from cai.workflows.parent_check import _scratch_deps

        deps = _scratch_deps()
        assert "parent-check-" in str(deps.backend.root_dir)


# ---------------------------------------------------------------------------
# FetchParentNode
# ---------------------------------------------------------------------------


class TestFetchParentNode:
    def test_no_parent_returns_end(self):
        """When the closed sub-issue has no parent, return End(None)."""
        state = _state()
        with patch(
            "cai.workflows.parent_check.get_parent_issue", return_value=None
        ):
            result = asyncio.run(
                FetchParentNode().run(GraphRunContext(state=state, deps=None))
            )
        assert isinstance(result, End)
        assert result.data is None

    def test_siblings_still_open_returns_end(self):
        """When any sibling sub-issue is still open, return End(None)."""
        state = _state()
        with (
            patch(
                "cai.workflows.parent_check.get_parent_issue", return_value=42
            ),
            patch(
                "cai.workflows.parent_check.list_sub_issues",
                return_value=[
                    {"state": "closed", "number": 1, "title": "Done"},
                    {"state": "open", "number": 2, "title": "Still open"},
                ],
            ),
        ):
            result = asyncio.run(
                FetchParentNode().run(GraphRunContext(state=state, deps=None))
            )
        assert isinstance(result, End)
        assert result.data is None

    def test_all_siblings_closed_proceeds(self):
        """When all sub-issues are closed, proceed to VerifyParentNode."""
        state = _state()
        mock_issue = MagicMock()
        mock_issue.body = "Parent issue body text."

        with (
            patch(
                "cai.workflows.parent_check.get_parent_issue", return_value=42
            ),
            patch(
                "cai.workflows.parent_check.list_sub_issues",
                return_value=[
                    {
                        "state": "closed",
                        "number": 1,
                        "title": "First task",
                        "state_reason": "completed",
                    },
                    {
                        "state": "closed",
                        "number": 2,
                        "title": "Second task",
                        "state_reason": "completed",
                    },
                ],
            ),
        ):
            state.bot.repo.return_value.get_issue.return_value = mock_issue
            result = asyncio.run(
                FetchParentNode().run(GraphRunContext(state=state, deps=None))
            )

        assert isinstance(result, VerifyParentNode)
        assert state.parent_number == 42
        assert state.parent_body == "Parent issue body text."
        assert "#1" in state.sub_issues_summary
        assert "#2" in state.sub_issues_summary
        assert "completed" in state.sub_issues_summary

    def test_parent_body_none_uses_empty_string(self):
        """When the GitHub API returns ``None`` for body, use ``""``."""
        state = _state()
        mock_issue = MagicMock()
        mock_issue.body = None

        with (
            patch(
                "cai.workflows.parent_check.get_parent_issue", return_value=42
            ),
            patch(
                "cai.workflows.parent_check.list_sub_issues",
                return_value=[
                    {"state": "closed", "number": 1, "title": "Done"},
                ],
            ),
        ):
            state.bot.repo.return_value.get_issue.return_value = mock_issue
            result = asyncio.run(
                FetchParentNode().run(GraphRunContext(state=state, deps=None))
            )

        assert isinstance(result, VerifyParentNode)
        assert state.parent_body == ""

    def test_sibling_missing_keys_uses_defaults(self):
        """Siblings with missing keys fall back to default values."""
        state = _state()
        mock_issue = MagicMock()
        mock_issue.body = "body"

        with (
            patch(
                "cai.workflows.parent_check.get_parent_issue", return_value=42
            ),
            patch(
                "cai.workflows.parent_check.list_sub_issues",
                return_value=[
                    {"state": "closed"},  # missing number, title, state_reason
                ],
            ),
        ):
            state.bot.repo.return_value.get_issue.return_value = mock_issue
            result = asyncio.run(
                FetchParentNode().run(GraphRunContext(state=state, deps=None))
            )

        assert isinstance(result, VerifyParentNode)
        assert "#?" in state.sub_issues_summary
        assert "(untitled)" in state.sub_issues_summary


# ---------------------------------------------------------------------------
# VerifyParentNode
# ---------------------------------------------------------------------------


class TestVerifyParentNode:
    def test_verify_fulfilled_closes_parent(self):
        """When the agent says all_fulfilled, close the parent and update labels."""
        state = _state(parent_number=42, parent_body="Parent plan")

        mock_output = ParentCheckOutput(
            all_fulfilled=True,
            reason="All steps covered.",
            new_sub_issues=[],
        )
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(
            return_value=MagicMock(output=mock_output)
        )
        mock_issue = MagicMock()

        with (
            patch(
                "cai.workflows.parent_check.parent_verifier_agent",
                return_value=mock_agent,
            ),
            patch(
                "cai.workflows.parent_check.set_label"
            ) as mock_set_label,
        ):
            state.bot.repo.return_value.get_issue.return_value = mock_issue
            result = asyncio.run(
                VerifyParentNode().run(
                    GraphRunContext(state=state, deps=None)
                )
            )

        assert isinstance(result, End)
        assert result.data is None
        mock_issue.edit.assert_called_once_with(
            state="closed", state_reason="completed"
        )
        # cai:raised removed, cai:pr-ready added
        mock_set_label.assert_any_call(
            state.bot, state.repo, 42, "cai:raised", False
        )
        mock_set_label.assert_any_call(
            state.bot, state.repo, 42, "cai:pr-ready", True
        )
        assert state.output is mock_output

    def test_verify_incomplete_creates_sub_issues(self):
        """When the agent says not fulfilled, create new sub-issues."""
        state = _state(parent_number=42, parent_body="Parent plan")

        mock_output = ParentCheckOutput(
            all_fulfilled=False,
            reason="Steps 2 and 3 not covered.",
            new_sub_issues=["Remaining A", "Remaining B"],
        )
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(
            return_value=MagicMock(output=mock_output)
        )

        created_issues = [MagicMock(id=101), MagicMock(id=102)]
        push_captures: list[dict] = []

        def _push_capture(bot, json_path):
            push_captures.append(json.loads(json_path.read_text()))
            return created_issues[len(push_captures) - 1]

        with (
            patch(
                "cai.workflows.parent_check.parent_verifier_agent",
                return_value=mock_agent,
            ),
            patch(
                "cai.workflows.parent_check.push",
                side_effect=_push_capture,
            ),
            patch(
                "cai.workflows.parent_check.add_sub_issue"
            ) as mock_add_sub,
        ):
            result = asyncio.run(
                VerifyParentNode().run(
                    GraphRunContext(state=state, deps=None)
                )
            )

        assert isinstance(result, End)
        assert result.data is None
        assert len(push_captures) == 2
        assert mock_add_sub.call_count == 2

        # First sub-issue: cai:sub-issue + cai:raised
        assert set(push_captures[0]["labels"]) == {"cai:sub-issue", "cai:raised"}
        assert push_captures[0]["title"] == "Remaining A"
        # Second sub-issue: cai:sub-issue only
        assert set(push_captures[1]["labels"]) == {"cai:sub-issue"}
        assert push_captures[1]["title"] == "Remaining B"

        mock_add_sub.assert_any_call(state.bot, state.repo, 42, 101)
        mock_add_sub.assert_any_call(state.bot, state.repo, 42, 102)

        assert state.output is mock_output

    def test_verify_parent_number_none_raises_assertion(self):
        """When ``parent_number`` is None, ``AssertionError`` is raised."""
        state = _state(parent_number=None, parent_body="Parent plan")

        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(
            return_value=MagicMock(output=MagicMock())
        )

        with patch(
            "cai.workflows.parent_check.parent_verifier_agent",
            return_value=mock_agent,
        ):
            with pytest.raises(AssertionError):
                asyncio.run(
                    VerifyParentNode().run(
                        GraphRunContext(state=state, deps=None)
                    )
                )

    def test_verify_incomplete_empty_list_no_errors(self):
        """When ``all_fulfilled`` is False but ``new_sub_issues`` is empty, no crash."""
        state = _state(parent_number=42, parent_body="Parent plan")

        mock_output = ParentCheckOutput(
            all_fulfilled=False,
            reason="Nothing left to do, but not auto-closing.",
            new_sub_issues=[],
        )
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(
            return_value=MagicMock(output=mock_output)
        )

        with (
            patch(
                "cai.workflows.parent_check.parent_verifier_agent",
                return_value=mock_agent,
            ),
            patch(
                "cai.workflows.parent_check.push",
            ),
            patch(
                "cai.workflows.parent_check.add_sub_issue",
            ),
        ):
            result = asyncio.run(
                VerifyParentNode().run(
                    GraphRunContext(state=state, deps=None)
                )
            )

        assert isinstance(result, End)
        assert result.data is None
        assert state.output is mock_output


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------


class TestParentCheckGraph:
    def test_is_pydantic_graph(self):
        from pydantic_graph import Graph
        assert isinstance(parent_check_graph, Graph)

    def test_contains_nodes(self):
        nodes = parent_check_graph.get_nodes()
        assert FetchParentNode in nodes
        assert VerifyParentNode in nodes

    def test_node_order_matches_workflow(self):
        """Nodes are ordered FetchParentNode → VerifyParentNode."""
        nodes = parent_check_graph.get_nodes()
        assert nodes == [FetchParentNode, VerifyParentNode]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestMain:
    def test_main_runs_without_error(self):
        """``main()`` parses args and runs the graph to completion."""
        with (
            patch("sys.argv", ["cai-parent-check", "owner/repo#99"]),
            patch(
                "cai.workflows.parent_check.asyncio.run"
            ) as mock_run,
            patch(
                "cai.workflows.parent_check.parse_ref_and_bot",
                return_value=(MagicMock(), "owner/repo", 99),
            ),
            patch(
                "cai.workflows.parent_check.langfuse_workflow"
            ) as mock_langfuse,
        ):
            mock_langfuse.return_value.__enter__ = MagicMock()
            mock_langfuse.return_value.__exit__ = MagicMock()
            main()
            mock_run.assert_called_once()

    def test_main_invalid_ref_exits(self):
        """``main()`` raises ``SystemExit`` on a malformed ref."""
        with (
            patch("sys.argv", ["cai-parent-check", "not-a-ref"]),
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
            pytest.raises(SystemExit),
        ):
            main()
        assert "expected owner/repo#number" in mock_stderr.getvalue()
