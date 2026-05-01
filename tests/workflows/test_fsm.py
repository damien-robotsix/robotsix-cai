from __future__ import annotations

import asyncio as _real_asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cai.github.issues import IssueMeta
from cai.workflows.state import IssueState


# ---------------------------------------------------------------------------
# Module integrity tests
# ---------------------------------------------------------------------------


class TestModuleIntegrity:
    def test_module_imports_without_error(self):
        """The fsm module must import cleanly — no stray tokens at module level."""
        import cai.workflows.fsm as fsm

        assert fsm.solve_graph is not None

    def test_solve_graph_surface(self):
        """solve_graph is a pydantic Graph with run and run_sync exposed."""
        from cai.workflows.fsm import solve_graph

        assert hasattr(solve_graph, "run")
        assert callable(solve_graph.run)
        assert hasattr(solve_graph, "run_sync")
        assert callable(solve_graph.run_sync)

    def test_solve_graph_includes_github_workflow_review(self):
        """solve_graph.nodes includes GitHubWorkflowReviewNode between PythonReviewNode and TestSanityNode."""
        from cai.workflows.fsm import solve_graph
        from cai.workflows.github_workflow_review import GitHubWorkflowReviewNode

        assert any(
            issubclass(defn.node, GitHubWorkflowReviewNode)
            for defn in solve_graph.node_defs.values()
        ), "GitHubWorkflowReviewNode must be registered in solve_graph.node_defs"


# ---------------------------------------------------------------------------
# solve_issue tests
# ---------------------------------------------------------------------------


def _build_solve_issue_mocks():
    """Return a dict of patcher start/stop helpers wired to the fsm module."""
    return {
        "langfuse": patch("cai.workflows.fsm.langfuse_workflow"),
        "ensure": patch("cai.workflows.fsm.ensure_labels"),
        "set_label": patch("cai.workflows.fsm.set_label"),
        "run": patch("cai.workflows.fsm.solve_graph.run", new_callable=AsyncMock),
    }


def _workspace_issue_files(tmp_path: Path):
    """Create minimal issue workspace files and return the paths."""
    root = tmp_path / "workspace"
    root.mkdir()
    json_path = root / "99.json"
    md_path = root / "99.md"
    repo_root = root / "repo"
    repo_root.mkdir()

    json_path.write_text(
        '{"repo": "o/r", "number": 99, "title": "Test issue", "labels": ["cai:raised"]}'
    )
    md_path.write_text("body")

    from cai.github.repo import IssueWorkspace

    return IssueWorkspace(
        root=root,
        issue_json=json_path,
        issue_md=md_path,
        repo_root=repo_root,
    )


def _mock_bot():
    """Return a MagicMock CaiBot with enough surface for solve_issue."""
    bot = MagicMock()
    bot.token_for.return_value = "tok"

    mock_issue = MagicMock()
    mock_issue.labels = [MagicMock(name="cai:raised")]
    mock_issue.labels[0].name = "cai:raised"

    mock_repo = MagicMock()
    mock_repo.get_issue.return_value = mock_issue

    bot.repo.return_value = mock_repo
    return bot


class TestSolveIssueHumanReviewLabel:
    def test_auto_merge_disabled_adds_label(self, tmp_path: Path):
        """When auto_merge_enabled is False, set_label('cai:human-review', present=True) is called."""
        mocks = _build_solve_issue_mocks()

        with mocks["langfuse"], mocks["ensure"] as mock_ensure, mocks["set_label"] as mock_set_label, mocks["run"] as mock_run:
            # Arrange: set auto_merge_enabled=False on the state during graph run
            async def set_state(*args, **kwargs):
                state = kwargs["state"]
                state.pr_number = 1
                state.pr_url = "https://github.com/o/r/pull/1"
                state.new_meta = IssueMeta(repo="o/r", number=99, title="t")
                state.auto_merge_enabled = False

            mock_run.side_effect = set_state

            bot = _mock_bot()
            workspace = _workspace_issue_files(tmp_path)

            from cai.workflows.fsm import solve_issue

            solve_issue(bot, workspace)

            # Assert: human-review label applied
            mock_set_label.assert_called_once_with(
                bot, "o/r", 1, "cai:human-review", present=True
            )

    def test_auto_merge_enabled_skips_label(self, tmp_path: Path):
        """When auto_merge_enabled is True, set_label('cai:human-review', present=True) is NOT called."""
        mocks = _build_solve_issue_mocks()

        with mocks["langfuse"], mocks["ensure"] as mock_ensure, mocks["set_label"] as mock_set_label, mocks["run"] as mock_run:
            async def set_state(*args, **kwargs):
                state = kwargs["state"]
                state.pr_number = 1
                state.pr_url = "https://github.com/o/r/pull/1"
                state.new_meta = IssueMeta(repo="o/r", number=99, title="t")
                state.auto_merge_enabled = True

            mock_run.side_effect = set_state

            bot = _mock_bot()
            workspace = _workspace_issue_files(tmp_path)

            from cai.workflows.fsm import solve_issue

            solve_issue(bot, workspace)

            # Assert: human-review label NOT applied
            set_label_calls = [
                c for c in mock_set_label.call_args_list
                if c.args[3] == "cai:human-review"
            ]
            assert len(set_label_calls) == 0

    def test_no_pr_number_skips_label_regardless(self, tmp_path: Path):
        """When no PR is opened (pr_number is None), set_label is never called."""
        mocks = _build_solve_issue_mocks()

        with mocks["langfuse"], mocks["ensure"] as mock_ensure, mocks["set_label"] as mock_set_label, mocks["run"] as mock_run:
            async def set_state(*args, **kwargs):
                state = kwargs["state"]
                state.pr_number = None
                state.pr_url = None
                state.new_meta = IssueMeta(repo="o/r", number=99, title="t")

            mock_run.side_effect = set_state

            bot = _mock_bot()
            workspace = _workspace_issue_files(tmp_path)

            from cai.workflows.fsm import solve_issue

            solve_issue(bot, workspace)

            # Assert: set_label never called at all
            mock_set_label.assert_not_called()


class TestSolveIssueAsyncioRun:
    """Tests that solve_issue wraps langfuse_workflow + graph in a single asyncio.run."""

    def test_asyncio_run_called_once(self, tmp_path: Path):
        """asyncio.run is called exactly once by solve_issue."""
        mocks = _build_solve_issue_mocks()

        with (
            mocks["langfuse"],
            mocks["ensure"] as mock_ensure,
            mocks["set_label"] as mock_set_label,
            mocks["run"] as mock_run,
        ):
            async def set_state(*args, **kwargs):
                state = kwargs["state"]
                state.pr_number = 1
                state.pr_url = "https://github.com/o/r/pull/1"
                state.new_meta = IssueMeta(repo="o/r", number=99, title="t")
                state.auto_merge_enabled = False

            mock_run.side_effect = set_state

            bot = _mock_bot()
            workspace = _workspace_issue_files(tmp_path)

            from cai.workflows.fsm import solve_issue

            # Patch asyncio.run in the fsm module to capture the call.
            # Snapshot the real asyncio.run *before* patching because the
            # patch replaces asyncio.run on the singleton asyncio module,
            # which is the same object as _real_asyncio.
            _real_run = _real_asyncio.run
            with patch("cai.workflows.fsm.asyncio.run") as mock_asyncio_run:
                mock_asyncio_run.side_effect = _real_run

                solve_issue(bot, workspace)

                mock_asyncio_run.assert_called_once()

    def test_langfuse_workflow_called_with_session_id(self, tmp_path: Path):
        """langfuse_workflow is entered with session_id='issue-99' for an issue run."""
        mocks = _build_solve_issue_mocks()

        with (
            mocks["langfuse"] as mock_langfuse,
            mocks["ensure"] as mock_ensure,
            mocks["set_label"] as mock_set_label,
            mocks["run"] as mock_run,
        ):
            async def set_state(*args, **kwargs):
                state = kwargs["state"]
                state.pr_number = 1
                state.pr_url = "https://github.com/o/r/pull/1"
                state.new_meta = IssueMeta(repo="o/r", number=99, title="t")
                state.auto_merge_enabled = False

            mock_run.side_effect = set_state

            bot = _mock_bot()
            workspace = _workspace_issue_files(tmp_path)

            from cai.workflows.fsm import solve_issue

            solve_issue(bot, workspace)

            mock_langfuse.assert_called_once_with(
                "cai-solve",
                input={"issue": "o/r#99", "title": "Test issue"},
                metadata={"repo": "o/r", "issue_number": 99},
                session_id="issue-99",
            )

    def test_graph_run_called_with_explore_node(self, tmp_path: Path):
        """solve_graph.run is called with ExploreNode() as the start node."""
        mocks = _build_solve_issue_mocks()

        with (
            mocks["langfuse"],
            mocks["ensure"] as mock_ensure,
            mocks["set_label"] as mock_set_label,
            mocks["run"] as mock_run,
        ):
            async def set_state(*args, **kwargs):
                state = kwargs["state"]
                state.pr_number = 1
                state.pr_url = "https://github.com/o/r/pull/1"
                state.new_meta = IssueMeta(repo="o/r", number=99, title="t")
                state.auto_merge_enabled = False

            mock_run.side_effect = set_state

            bot = _mock_bot()
            workspace = _workspace_issue_files(tmp_path)

            from cai.workflows.fsm import solve_issue
            from cai.workflows.explore import ExploreNode

            solve_issue(bot, workspace)

            assert mock_run.call_count >= 1
            args, kwargs = mock_run.call_args
            assert isinstance(args[0], ExploreNode)
            assert kwargs.get("state") is not None


class TestSolveIssueAgentRunError:
    """Tests that solve_issue catches AgentRunError and applies cai:failed label."""

    def test_agent_run_error_applies_cai_failed_label(self, tmp_path: Path):
        """When AgentRunError is raised, ensure_labels is called and issue.edit removes cai:raised, adds cai:failed."""
        from pydantic_ai.exceptions import AgentRunError

        mocks = _build_solve_issue_mocks()

        with (
            mocks["langfuse"],
            mocks["ensure"] as mock_ensure,
            mocks["set_label"] as mock_set_label,
            mocks["run"] as mock_run,
        ):
            mock_run.side_effect = AgentRunError("Usage limit exceeded")

            bot = _mock_bot()
            workspace = _workspace_issue_files(tmp_path)

            from cai.github.labels import CAI_LABEL_SPECS
            from cai.workflows.fsm import solve_issue

            with pytest.raises(AgentRunError):
                solve_issue(bot, workspace)

            # ensure_labels was called with CAI_LABEL_SPECS
            mock_ensure.assert_called_once_with(bot, "o/r", CAI_LABEL_SPECS)

            # The mock issue was fetched and edited with correct labels
            issue = bot.repo("o/r").get_issue(99)
            issue.edit.assert_called_once()
            _args, kwargs = issue.edit.call_args
            assert "cai:raised" not in kwargs["labels"]
            assert "cai:failed" in kwargs["labels"]

    def test_agent_run_error_re_raised(self, tmp_path: Path):
        """AgentRunError propagates to the caller after label handling."""
        from pydantic_ai.exceptions import AgentRunError

        mocks = _build_solve_issue_mocks()

        with (
            mocks["langfuse"],
            mocks["ensure"] as mock_ensure,
            mocks["set_label"] as mock_set_label,
            mocks["run"] as mock_run,
        ):
            mock_run.side_effect = AgentRunError("Usage limit exceeded")

            bot = _mock_bot()
            workspace = _workspace_issue_files(tmp_path)

            from cai.workflows.fsm import solve_issue

            with pytest.raises(AgentRunError):
                solve_issue(bot, workspace)

    def test_agent_run_error_without_issue_number(self, tmp_path: Path):
        """When meta.number is None, labels are not modified but exception still propagates."""
        from pydantic_ai.exceptions import AgentRunError

        mocks = _build_solve_issue_mocks()

        with (
            mocks["langfuse"],
            mocks["ensure"] as mock_ensure,
            mocks["set_label"] as mock_set_label,
            mocks["run"] as mock_run,
        ):
            mock_run.side_effect = AgentRunError("Usage limit exceeded")

            bot = _mock_bot()

            # Create workspace where issue_json has null number
            root = tmp_path / "workspace_no_number"
            root.mkdir()
            json_path = root / "0.json"
            md_path = root / "0.md"
            repo_root = root / "repo"
            repo_root.mkdir()
            json_path.write_text(
                '{"repo": "o/r", "number": null, "title": "No number issue", "labels": []}'
            )
            md_path.write_text("body")

            from cai.github.repo import IssueWorkspace

            workspace = IssueWorkspace(
                root=root,
                issue_json=json_path,
                issue_md=md_path,
                repo_root=repo_root,
            )

            from cai.workflows.fsm import solve_issue

            with pytest.raises(AgentRunError):
                solve_issue(bot, workspace)

            # Labels must not have been touched when meta.number is None
            mock_ensure.assert_not_called()
            mock_set_label.assert_not_called()


# ---------------------------------------------------------------------------
# solve_pr tests
# ---------------------------------------------------------------------------


def _build_solve_pr_mocks():
    """Return a dict of patcher start/stop helpers for solve_pr."""
    return {
        "langfuse": patch("cai.workflows.fsm.langfuse_workflow"),
        "set_label": patch("cai.workflows.fsm.set_label"),
        "run": patch("cai.workflows.fsm.solve_graph.run", new_callable=AsyncMock),
        "unresolved": patch("cai.workflows.fsm.list_unresolved_threads", return_value=[]),
        "resolved": patch("cai.workflows.fsm.list_resolved_threads", return_value=[]),
        "session_id": patch("cai.workflows.registry.session_id_for_pr", return_value="sess"),
    }


def _workspace_pr_files(tmp_path: Path):
    """Create minimal PR workspace files and return a PRWorkspace."""
    root = tmp_path / "pr_workspace"
    root.mkdir()
    repo_root = root / "repo"
    repo_root.mkdir()
    body_path = root / "99.md"
    body_path.write_text("body")

    from cai.github.repo import PRWorkspace

    return PRWorkspace(
        root=root,
        repo_root=repo_root,
        body_path=body_path,
        repo="o/r",
        number=99,
        head_branch="feature/x",
        base_branch="main",
        title="PR title",
        body="PR body",
    )


class TestSolvePRHumanReviewLabel:
    def test_auto_merge_disabled_adds_label(self, tmp_path: Path):
        """When auto_merge_enabled is False, set_label('cai:human-review', present=True) is called."""
        mocks = _build_solve_pr_mocks()

        with (
            mocks["langfuse"],
            mocks["set_label"] as mock_set_label,
            mocks["run"] as mock_run,
            mocks["unresolved"],
            mocks["resolved"],
            mocks["session_id"],
        ):
            async def set_state(*args, **kwargs):
                state = kwargs["state"]
                state.auto_merge_enabled = False

            mock_run.side_effect = set_state

            bot = _mock_bot()
            workspace = _workspace_pr_files(tmp_path)

            from cai.workflows.fsm import solve_pr

            solve_pr(bot, workspace)

            # Assert: human-review label applied (as the second call — first
            # is the unconditional removal at the top of solve_pr)
            human_review_calls = [
                c for c in mock_set_label.call_args_list
                if c.args[3] == "cai:human-review"
            ]
            assert len(human_review_calls) == 2  # one removal, one addition
            # First call: present=False (removal)
            assert human_review_calls[0].kwargs == {"present": False}
            # Second call: present=True (addition)
            assert human_review_calls[1].kwargs == {"present": True}

    def test_auto_merge_enabled_skips_label(self, tmp_path: Path):
        """When auto_merge_enabled is True, only the removal call fires."""
        mocks = _build_solve_pr_mocks()

        with (
            mocks["langfuse"],
            mocks["set_label"] as mock_set_label,
            mocks["run"] as mock_run,
            mocks["unresolved"],
            mocks["resolved"],
            mocks["session_id"],
        ):
            async def set_state(*args, **kwargs):
                state = kwargs["state"]
                state.auto_merge_enabled = True

            mock_run.side_effect = set_state

            bot = _mock_bot()
            workspace = _workspace_pr_files(tmp_path)

            from cai.workflows.fsm import solve_pr

            solve_pr(bot, workspace)

            # Assert: only the removal call for human-review
            human_review_calls = [
                c for c in mock_set_label.call_args_list
                if c.args[3] == "cai:human-review"
            ]
            assert len(human_review_calls) == 1
            assert human_review_calls[0].kwargs == {"present": False}


class TestSolvePRAsyncioRun:
    """Tests that solve_pr wraps langfuse_workflow + graph in a single asyncio.run."""

    def test_asyncio_run_called_once(self, tmp_path: Path):
        """asyncio.run is called exactly once by solve_pr."""
        mocks = _build_solve_pr_mocks()

        with (
            mocks["langfuse"],
            mocks["set_label"] as mock_set_label,
            mocks["run"] as mock_run,
            mocks["unresolved"],
            mocks["resolved"],
            mocks["session_id"],
        ):
            async def set_state(*args, **kwargs):
                state = kwargs["state"]
                state.auto_merge_enabled = False

            mock_run.side_effect = set_state

            bot = _mock_bot()
            workspace = _workspace_pr_files(tmp_path)

            from cai.workflows.fsm import solve_pr

            _real_run = _real_asyncio.run
            with patch("cai.workflows.fsm.asyncio.run") as mock_asyncio_run:
                mock_asyncio_run.side_effect = _real_run

                solve_pr(bot, workspace)

                mock_asyncio_run.assert_called_once()

    def test_langfuse_workflow_called_with_session_id(self, tmp_path: Path):
        """langfuse_workflow is entered with session_id from session_id_for_pr."""
        mocks = _build_solve_pr_mocks()

        with (
            mocks["langfuse"] as mock_langfuse,
            mocks["set_label"] as mock_set_label,
            mocks["run"] as mock_run,
            mocks["unresolved"],
            mocks["resolved"],
            mocks["session_id"] as mock_session_id,
        ):
            async def set_state(*args, **kwargs):
                state = kwargs["state"]
                state.auto_merge_enabled = False

            mock_run.side_effect = set_state

            bot = _mock_bot()
            workspace = _workspace_pr_files(tmp_path)

            from cai.workflows.fsm import solve_pr

            solve_pr(bot, workspace)

            # session_id_for_pr was consulted
            mock_session_id.assert_called_once_with(99, "feature/x")

            # langfuse_workflow receives the mocked session_id
            mock_langfuse.assert_called_once_with(
                "cai-solve",
                input={"pr": "o/r#99", "title": "PR title", "branch": "feature/x"},
                metadata={"repo": "o/r", "pr_number": 99},
                session_id="sess",
            )

    def test_graph_run_called_with_implement_node(self, tmp_path: Path):
        """solve_graph.run is called with ImplementNode() as the start node."""
        mocks = _build_solve_pr_mocks()

        with (
            mocks["langfuse"],
            mocks["set_label"] as mock_set_label,
            mocks["run"] as mock_run,
            mocks["unresolved"],
            mocks["resolved"],
            mocks["session_id"],
        ):
            async def set_state(*args, **kwargs):
                state = kwargs["state"]
                state.auto_merge_enabled = False

            mock_run.side_effect = set_state

            bot = _mock_bot()
            workspace = _workspace_pr_files(tmp_path)

            from cai.workflows.fsm import solve_pr
            from cai.workflows.implement import ImplementNode

            solve_pr(bot, workspace)

            assert mock_run.call_count >= 1
            args, kwargs = mock_run.call_args
            assert isinstance(args[0], ImplementNode)
            assert kwargs.get("state") is not None
