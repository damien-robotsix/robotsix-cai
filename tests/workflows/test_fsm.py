from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

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
        """solve_graph is a pydantic Graph with run_sync exposed."""
        from cai.workflows.fsm import solve_graph

        assert hasattr(solve_graph, "run_sync")
        assert callable(solve_graph.run_sync)


# ---------------------------------------------------------------------------
# solve_issue tests
# ---------------------------------------------------------------------------


def _build_solve_issue_mocks():
    """Return a dict of patcher start/stop helpers wired to the fsm module."""
    return {
        "langfuse": patch("cai.workflows.fsm.langfuse_workflow"),
        "ensure": patch("cai.workflows.fsm.ensure_labels"),
        "set_label": patch("cai.workflows.fsm.set_label"),
        "run_sync": patch("cai.workflows.fsm.solve_graph.run_sync"),
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

        with mocks["langfuse"], mocks["ensure"] as mock_ensure, mocks["set_label"] as mock_set_label, mocks["run_sync"] as mock_run_sync:
            # Arrange: set auto_merge_enabled=False on the state during graph run
            def set_state(*args, **kwargs):
                state = kwargs["state"]
                state.pr_number = 1
                state.pr_url = "https://github.com/o/r/pull/1"
                state.new_meta = IssueMeta(repo="o/r", number=99, title="t")
                state.auto_merge_enabled = False

            mock_run_sync.side_effect = set_state

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

        with mocks["langfuse"], mocks["ensure"] as mock_ensure, mocks["set_label"] as mock_set_label, mocks["run_sync"] as mock_run_sync:
            def set_state(*args, **kwargs):
                state = kwargs["state"]
                state.pr_number = 1
                state.pr_url = "https://github.com/o/r/pull/1"
                state.new_meta = IssueMeta(repo="o/r", number=99, title="t")
                state.auto_merge_enabled = True

            mock_run_sync.side_effect = set_state

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

        with mocks["langfuse"], mocks["ensure"] as mock_ensure, mocks["set_label"] as mock_set_label, mocks["run_sync"] as mock_run_sync:
            def set_state(*args, **kwargs):
                state = kwargs["state"]
                state.pr_number = None
                state.pr_url = None
                state.new_meta = IssueMeta(repo="o/r", number=99, title="t")

            mock_run_sync.side_effect = set_state

            bot = _mock_bot()
            workspace = _workspace_issue_files(tmp_path)

            from cai.workflows.fsm import solve_issue

            solve_issue(bot, workspace)

            # Assert: set_label never called at all
            mock_set_label.assert_not_called()


# ---------------------------------------------------------------------------
# solve_pr tests
# ---------------------------------------------------------------------------


def _build_solve_pr_mocks():
    """Return a dict of patcher start/stop helpers for solve_pr."""
    return {
        "langfuse": patch("cai.workflows.fsm.langfuse_workflow"),
        "set_label": patch("cai.workflows.fsm.set_label"),
        "run_sync": patch("cai.workflows.fsm.solve_graph.run_sync"),
        "unresolved": patch("cai.workflows.fsm.list_unresolved_threads", return_value=[]),
        "resolved": patch("cai.workflows.fsm.list_resolved_threads", return_value=[]),
        "session_id": patch("cai.workflows.fsm.session_id_for_pr", return_value="sess"),
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
            mocks["run_sync"] as mock_run_sync,
            mocks["unresolved"],
            mocks["resolved"],
            mocks["session_id"],
        ):
            def set_state(*args, **kwargs):
                state = kwargs["state"]
                state.auto_merge_enabled = False

            mock_run_sync.side_effect = set_state

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
            mocks["run_sync"] as mock_run_sync,
            mocks["unresolved"],
            mocks["resolved"],
            mocks["session_id"],
        ):
            def set_state(*args, **kwargs):
                state = kwargs["state"]
                state.auto_merge_enabled = True

            mock_run_sync.side_effect = set_state

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
