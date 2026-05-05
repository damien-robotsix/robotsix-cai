from __future__ import annotations

import asyncio as _real_asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cai.github.issues import IssueMeta
from cai.workflows.state import IssueState, SessionState


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

    def test_solve_graph_includes_pydantic_ai_review(self):
        """solve_graph.nodes includes PydanticAIReviewNode between GitHubWorkflowReviewNode and TestSanityNode."""
        from cai.workflows.fsm import solve_graph
        from cai.workflows.pydantic_ai_review import PydanticAIReviewNode

        assert any(
            issubclass(defn.node, PydanticAIReviewNode)
            for defn in solve_graph.node_defs.values()
        ), "PydanticAIReviewNode must be registered in solve_graph.node_defs"

    def test_solve_graph_node_ordering(self):
        """solve_graph.nodes are in the expected order: ... PythonReview, GitHubWorkflowReview, PydanticAIReview, TestSanity ..."""
        from cai.workflows.fsm import solve_graph
        from cai.workflows.github_workflow_review import GitHubWorkflowReviewNode
        from cai.workflows.pydantic_ai_review import PydanticAIReviewNode
        from cai.workflows.python_review import PythonReviewNode
        from cai.workflows.test_runner import TestSanityNode

        # Get the ordered list of node classes from the graph's internal defs.
        # pydantic_graph stores them in insertion order.
        node_classes = [defn.node for defn in solve_graph.node_defs.values()]

        # Find the indices of the adjacent nodes we care about.
        python_idx = next(i for i, n in enumerate(node_classes) if issubclass(n, PythonReviewNode))
        gh_idx = next(i for i, n in enumerate(node_classes) if issubclass(n, GitHubWorkflowReviewNode))
        ai_idx = next(i for i, n in enumerate(node_classes) if issubclass(n, PydanticAIReviewNode))
        sanity_idx = next(i for i, n in enumerate(node_classes) if issubclass(n, TestSanityNode))

        assert python_idx < gh_idx < ai_idx < sanity_idx, (
            f"Expected PythonReview({python_idx}) < GitHubWorkflowReview({gh_idx}) "
            f"< PydanticAIReview({ai_idx}) < TestSanity({sanity_idx})"
        )


# ---------------------------------------------------------------------------
# solve_issue tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stub_get_issue_type():
    """Default every solve_issue test to the code-change flow (no Type field).

    Prevents a real HTTPS call to GitHub's GraphQL endpoint when fsm reads
    the project Type. Tests that need the analysis branch override this
    via their own ``patch("cai.workflows.fsm.get_issue_type", ...)``.
    """
    with patch("cai.workflows.fsm.get_issue_type", return_value=None) as p:
        yield p


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


class TestSolveIssueSessionState:
    """Tests that solve_issue loads session state from the workspace root."""

    def test_session_state_loaded_from_workspace_root(self, tmp_path: Path):
        """solve_issue loads session_state from the body_path parent directory."""
        mocks = _build_solve_issue_mocks()

        with (
            mocks["langfuse"],
            mocks["ensure"] as mock_ensure,
            mocks["set_label"] as mock_set_label,
            mocks["run"] as mock_run,
        ):
            captured_state = None

            async def set_state(*args, **kwargs):
                nonlocal captured_state
                captured_state = kwargs["state"]
                state = kwargs["state"]
                state.pr_number = 1
                state.pr_url = "https://github.com/o/r/pull/1"
                state.new_meta = IssueMeta(repo="o/r", number=99, title="t")
                state.auto_merge_enabled = False

            mock_run.side_effect = set_state

            bot = _mock_bot()
            workspace = _workspace_issue_files(tmp_path)

            # Write a session_state.json next to the issue files
            session_file = workspace.root / "session_state.json"
            session_file.write_text(
                '{\n'
                '  "explore_findings": "Prior findings.",\n'
                '  "explore_files": ["src/prior.py"],\n'
                '  "known_corruptions": [],\n'
                '  "attempt_count": 2,\n'
                '  "prior_file_hashes": {}\n'
                '}'
            )

            from cai.workflows.fsm import solve_issue

            solve_issue(bot, workspace)

            assert captured_state is not None
            assert captured_state.session_state is not None
            assert captured_state.session_state.explore_findings == "Prior findings."
            assert captured_state.session_state.explore_files == ["src/prior.py"]
            assert captured_state.session_state.attempt_count == 2

    def test_session_state_defaults_when_no_file(self, tmp_path: Path):
        """When session_state.json does not exist, session_state is a default instance."""
        mocks = _build_solve_issue_mocks()

        with (
            mocks["langfuse"],
            mocks["ensure"] as mock_ensure,
            mocks["set_label"] as mock_set_label,
            mocks["run"] as mock_run,
        ):
            captured_state = None

            async def set_state(*args, **kwargs):
                nonlocal captured_state
                captured_state = kwargs["state"]
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

            assert captured_state is not None
            assert captured_state.session_state is not None
            # Default values after load_session_state with no file
            assert captured_state.session_state.attempt_count == 0
            assert captured_state.session_state.explore_findings == ""
            assert captured_state.session_state.explore_files == []


class TestSolveIssueFailureLabel:
    """Tests that solve_issue applies cai:failed when the graph raises."""

    def test_graph_exception_applies_failed_label(self, tmp_path: Path):
        """When solve_graph.run raises, cai:failed is applied and cai:raised removed."""
        mocks = _build_solve_issue_mocks()

        with (
            mocks["langfuse"],
            mocks["ensure"] as mock_ensure,
            mocks["set_label"] as mock_set_label,
            mocks["run"] as mock_run,
        ):
            # Arrange: the graph raises a RuntimeError
            mock_run.side_effect = RuntimeError("graph exploded")

            bot = _mock_bot()
            workspace = _workspace_issue_files(tmp_path)

            from cai.workflows.fsm import solve_issue

            with pytest.raises(RuntimeError, match="graph exploded"):
                solve_issue(bot, workspace)

            # Assert: ensure_labels was called so cai:failed label spec exists
            mock_ensure.assert_called_once()

            # Assert: issue.edit was called with labels containing cai:failed
            # and NOT containing cai:raised
            mock_issue = bot.repo.return_value.get_issue.return_value
            mock_issue.edit.assert_called_once()
            edit_call_labels = mock_issue.edit.call_args.kwargs["labels"]
            assert "cai:failed" in edit_call_labels
            assert "cai:raised" not in edit_call_labels

            # Assert: set_label (for human-review) was never called
            mock_set_label.assert_not_called()

    def test_graph_exception_without_issue_number_skips_label(self, tmp_path: Path):
        """When meta.number is None and the graph raises, no label edit occurs."""
        mocks = _build_solve_issue_mocks()

        with (
            mocks["langfuse"],
            mocks["ensure"] as mock_ensure,
            mocks["set_label"] as mock_set_label,
            mocks["run"] as mock_run,
        ):
            mock_run.side_effect = RuntimeError("graph exploded")

            bot = _mock_bot()
            workspace = _workspace_issue_files(tmp_path)
            # Modify the issue JSON to have no number
            workspace.issue_json.write_text(
                '{"repo": "o/r", "number": null, "title": "Test issue", "labels": ["cai:raised"]}'
            )

            from cai.workflows.fsm import solve_issue

            with pytest.raises(RuntimeError, match="graph exploded"):
                solve_issue(bot, workspace)

            # Assert: ensure_labels and issue.edit never called
            mock_ensure.assert_not_called()
            mock_set_label.assert_not_called()


class TestSolveIssueAnalysisFlow:
    """Project Type=analysis routes to the comment flow, skipping post-graph labels."""

    def test_analysis_type_sets_flow_kind(self, tmp_path: Path):
        """When get_issue_type returns 'analysis', state.flow_kind is set accordingly."""
        mocks = _build_solve_issue_mocks()

        with (
            mocks["langfuse"],
            mocks["ensure"],
            mocks["set_label"],
            mocks["run"] as mock_run,
            patch("cai.workflows.fsm.get_issue_type", return_value="analysis"),
        ):
            captured_state = None

            async def set_state(*args, **kwargs):
                nonlocal captured_state
                captured_state = kwargs["state"]
                state = kwargs["state"]
                state.new_meta = IssueMeta(repo="o/r", number=99, title="t")
                # CommentNode would have set comment_url before End.
                state.comment_url = "https://github.com/o/r/issues/100#issuecomment-1"

            mock_run.side_effect = set_state

            bot = _mock_bot()
            workspace = _workspace_issue_files(tmp_path)

            from cai.workflows.fsm import solve_issue

            new_meta, pr_url, comment_url = solve_issue(bot, workspace)

            assert captured_state is not None
            assert captured_state.flow_kind == "analysis"
            assert pr_url is None
            assert comment_url == "https://github.com/o/r/issues/100#issuecomment-1"

    def test_analysis_type_skips_post_graph_label_edit(self, tmp_path: Path):
        """For analysis flow, fsm does NOT touch labels — CommentNode owns that."""
        mocks = _build_solve_issue_mocks()

        with (
            mocks["langfuse"],
            mocks["ensure"] as mock_ensure,
            mocks["set_label"] as mock_set_label,
            mocks["run"] as mock_run,
            patch("cai.workflows.fsm.get_issue_type", return_value="analysis"),
        ):
            async def set_state(*args, **kwargs):
                state = kwargs["state"]
                state.new_meta = IssueMeta(repo="o/r", number=99, title="t")
                state.comment_url = "https://example/c"

            mock_run.side_effect = set_state

            bot = _mock_bot()
            workspace = _workspace_issue_files(tmp_path)

            from cai.workflows.fsm import solve_issue

            solve_issue(bot, workspace)

            # No post-graph ensure_labels / set_label / issue.edit from fsm.
            mock_ensure.assert_not_called()
            mock_set_label.assert_not_called()
            mock_issue = bot.repo.return_value.get_issue.return_value
            mock_issue.edit.assert_not_called()

    def test_unknown_type_defaults_to_code_change(self, tmp_path: Path):
        """An unrecognised Type value (e.g. 'experiment') doesn't activate analysis."""
        mocks = _build_solve_issue_mocks()

        with (
            mocks["langfuse"],
            mocks["ensure"],
            mocks["set_label"],
            mocks["run"] as mock_run,
            patch("cai.workflows.fsm.get_issue_type", return_value="experiment"),
        ):
            captured_state = None

            async def set_state(*args, **kwargs):
                nonlocal captured_state
                captured_state = kwargs["state"]
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

            assert captured_state.flow_kind == "code-change"


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
