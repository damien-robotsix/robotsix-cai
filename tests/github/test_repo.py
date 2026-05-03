"""Tests for :mod:`cai.github.repo` — workspace preparation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cai.github.repo import (
    IssueWorkspace,
    PRWorkspace,
    WORKSPACE_ROOT,
    PR_WORKSPACE_ROOT,
    parse_issue_ref,
    parse_pr_ref,
    issue_workspace,
    pr_workspace,
    prepare_workspace,
    prepare_pr_workspace,
    is_pull_request,
)


# ── Pure / path-computation helpers ─────────────────────────────────────


class TestParseIssueRef:
    def test_valid(self) -> None:
        assert parse_issue_ref("owner/repo#42") == ("owner/repo", 42)

    def test_valid_with_hyphens(self) -> None:
        assert parse_issue_ref("my-org/my-repo#7") == ("my-org/my-repo", 7)

    def test_no_match(self) -> None:
        assert parse_issue_ref("not-a-ref") is None

    def test_missing_number(self) -> None:
        assert parse_issue_ref("owner/repo#") is None

    def test_empty_string(self) -> None:
        assert parse_issue_ref("") is None

    def test_only_hash(self) -> None:
        assert parse_issue_ref("#") is None


class TestParsePrRef:
    def test_is_alias(self) -> None:
        assert parse_pr_ref is parse_issue_ref

    def test_valid(self) -> None:
        assert parse_pr_ref("owner/repo#123") == ("owner/repo", 123)


class TestIssueWorkspace:
    def test_returns_correct_path(self) -> None:
        path = issue_workspace("owner/name", 99)
        assert path == WORKSPACE_ROOT / "owner" / "name" / "99"

    def test_returns_correct_path_with_hyphens(self) -> None:
        path = issue_workspace("my-org/my-repo", 1)
        assert path == WORKSPACE_ROOT / "my-org" / "my-repo" / "1"


class TestPrWorkspace:
    def test_returns_correct_path(self) -> None:
        path = pr_workspace("owner/name", 99)
        assert path == PR_WORKSPACE_ROOT / "owner" / "name" / "99"

    def test_returns_correct_path_with_hyphens(self) -> None:
        path = pr_workspace("my-org/my-repo", 1)
        assert path == PR_WORKSPACE_ROOT / "my-org" / "my-repo" / "1"


# ── Dataclass construction ─────────────────────────────────────────────


class TestIssueWorkspaceDataclass:
    def test_fields(self) -> None:
        ws = IssueWorkspace(
            root=Path("/a"),
            issue_json=Path("/a/1.json"),
            issue_md=Path("/a/1.md"),
            repo_root=Path("/a/repo"),
        )
        assert ws.root == Path("/a")
        assert ws.issue_json == Path("/a/1.json")
        assert ws.issue_md == Path("/a/1.md")
        assert ws.repo_root == Path("/a/repo")

    def test_frozen(self) -> None:
        ws = IssueWorkspace(
            root=Path("/a"),
            issue_json=Path("/a/1.json"),
            issue_md=Path("/a/1.md"),
            repo_root=Path("/a/repo"),
        )
        with pytest.raises(AttributeError):
            ws.root = Path("/b")  # type: ignore[misc]


class TestPRWorkspaceDataclass:
    def test_fields(self) -> None:
        ws = PRWorkspace(
            root=Path("/a"),
            repo_root=Path("/a/repo"),
            body_path=Path("/a/1.md"),
            repo="owner/repo",
            number=1,
            head_branch="feature",
            base_branch="main",
            title="PR title",
            body="PR body",
        )
        assert ws.root == Path("/a")
        assert ws.repo_root == Path("/a/repo")
        assert ws.body_path == Path("/a/1.md")
        assert ws.repo == "owner/repo"
        assert ws.number == 1
        assert ws.head_branch == "feature"
        assert ws.base_branch == "main"
        assert ws.title == "PR title"
        assert ws.body == "PR body"

    def test_frozen(self) -> None:
        ws = PRWorkspace(
            root=Path("/a"),
            repo_root=Path("/a/repo"),
            body_path=Path("/a/1.md"),
            repo="owner/repo",
            number=1,
            head_branch="feature",
            base_branch="main",
            title="title",
            body="body",
        )
        with pytest.raises(AttributeError):
            ws.root = Path("/b")  # type: ignore[misc]


# ── is_pull_request ────────────────────────────────────────────────────


class TestIsPullRequest:
    def test_returns_true_when_pr(self) -> None:
        bot = MagicMock()
        issue = MagicMock()
        issue.pull_request = {"url": "..."}
        bot.repo.return_value.get_issue.return_value = issue

        result = is_pull_request(bot, "owner/repo", 42)

        assert result is True
        bot.repo.assert_called_once_with("owner/repo")
        bot.repo.return_value.get_issue.assert_called_once_with(42)

    def test_returns_false_when_issue(self) -> None:
        bot = MagicMock()
        issue = MagicMock()
        issue.pull_request = None
        bot.repo.return_value.get_issue.return_value = issue

        result = is_pull_request(bot, "owner/repo", 42)

        assert result is False


# ── prepare_workspace ──────────────────────────────────────────────────


class TestPrepareWorkspace:
    """Tests use ``tmp_path`` patched in as the workspace root so that
    filesystem operations like ``root.mkdir`` and ``json_path.exists()``
    work without touching ``/tmp/cai-solve``."""

    @pytest.fixture
    def patch_workspace_root(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        """Redirect ``WORKSPACE_ROOT`` to a temp directory."""
        ws_root = tmp_path / "cai-solve"
        monkeypatch.setattr("cai.github.repo.WORKSPACE_ROOT", ws_root)
        return ws_root

    def test_creates_dirs_and_pulls_issue(
        self, patch_workspace_root: Path
    ) -> None:
        bot = MagicMock()
        ws_root = patch_workspace_root

        with (
            patch("cai.github.repo.pull") as mock_pull,
            patch("cai.github.repo.clone") as mock_clone,
            patch("cai.github.repo.set_local") as mock_set_local,
        ):
            result = prepare_workspace(bot, "owner/name", 99)

        expected_root = ws_root / "owner" / "name" / "99"
        assert result.root == expected_root
        assert result.issue_json == expected_root / "99.json"
        assert result.issue_md == expected_root / "99.md"
        assert result.repo_root == expected_root / "repo"

        # Issue pull should be called because json didn't exist yet.
        mock_pull.assert_called_once_with(bot, "owner/name", 99, expected_root)
        # Clone should be called because repo dir didn't exist yet.
        mock_clone.assert_called_once_with(
            "https://github.com/owner/name.git",
            expected_root / "repo",
            env={"GIT_TERMINAL_PROMPT": "0"},
        )
        # Identity should be configured.
        mock_set_local.assert_called()

    def test_idempotent_when_workspace_exists(
        self, patch_workspace_root: Path
    ) -> None:
        """Second call with existing files should skip pull and clone."""
        ws_root = patch_workspace_root
        ws_dir = ws_root / "owner" / "name" / "99"
        ws_dir.mkdir(parents=True)
        (ws_dir / "99.json").write_text("{}")
        repo_dir = ws_dir / "repo"
        repo_dir.mkdir(parents=True)

        bot = MagicMock()
        with (
            patch("cai.github.repo.pull") as mock_pull,
            patch("cai.github.repo.clone") as mock_clone,
            patch("cai.github.repo.set_local") as mock_set_local,
        ):
            result = prepare_workspace(bot, "owner/name", 99)

        assert result.root == ws_dir
        mock_pull.assert_not_called()
        mock_clone.assert_not_called()
        # Identity is still written every call.
        mock_set_local.assert_called()

    def test_idempotent_when_issue_json_exists_but_no_clone(
        self, patch_workspace_root: Path
    ) -> None:
        """Existing issue metadata skips the pull but still clones."""
        ws_root = patch_workspace_root
        ws_dir = ws_root / "owner" / "name" / "99"
        ws_dir.mkdir(parents=True)
        (ws_dir / "99.json").write_text("{}")
        # repo dir does NOT exist.

        bot = MagicMock()
        with (
            patch("cai.github.repo.pull") as mock_pull,
            patch("cai.github.repo.clone") as mock_clone,
            patch("cai.github.repo.set_local") as mock_set_local,
        ):
            result = prepare_workspace(bot, "owner/name", 99)

        assert result.root == ws_dir
        mock_pull.assert_not_called()
        mock_clone.assert_called_once()
        mock_set_local.assert_called()

    def test_configures_identity(
        self, patch_workspace_root: Path
    ) -> None:
        bot = MagicMock()
        bot.bot_login = "my-bot[bot]"
        bot.app_id = 123456
        ws_root = patch_workspace_root

        with (
            patch("cai.github.repo.pull"),
            patch("cai.github.repo.clone"),
            patch("cai.github.repo.set_local") as mock_set_local,
        ):
            prepare_workspace(bot, "owner/name", 99)

        expected_repo_root = ws_root / "owner" / "name" / "99" / "repo"
        assert mock_set_local.call_count == 2
        mock_set_local.assert_any_call(
            "user.name", "my-bot[bot]", repo_root=expected_repo_root
        )
        mock_set_local.assert_any_call(
            "user.email",
            "123456+my-bot[bot]@users.noreply.github.com",
            repo_root=expected_repo_root,
        )


# ── prepare_pr_workspace ───────────────────────────────────────────────


class TestPreparePrWorkspace:
    @pytest.fixture
    def patch_pr_workspace_root(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        ws_root = tmp_path / "cai-solve-pr"
        monkeypatch.setattr("cai.github.repo.PR_WORKSPACE_ROOT", ws_root)
        return ws_root

    def test_creates_workspace_and_clones(
        self, patch_pr_workspace_root: Path
    ) -> None:
        bot = MagicMock()
        ws_root = patch_pr_workspace_root

        with (
            patch("cai.github.repo.get_pr_meta") as mock_get_pr_meta,
            patch("cai.github.repo.clone") as mock_clone,
            patch("cai.github.repo.set_local") as mock_set_local,
        ):
            mock_get_pr_meta.return_value = (
                "PR Title",
                "PR body text",
                "feature-branch",
                "main",
            )
            result = prepare_pr_workspace(bot, "owner/repo", 42)

        expected_root = ws_root / "owner" / "repo" / "42"
        assert result.root == expected_root
        assert result.repo_root == expected_root / "repo"
        assert result.body_path == expected_root / "42.md"
        assert result.repo == "owner/repo"
        assert result.number == 42
        assert result.head_branch == "feature-branch"
        assert result.base_branch == "main"
        assert result.title == "PR Title"
        assert result.body == "PR body text"

        mock_get_pr_meta.assert_called_once_with(bot, "owner/repo", 42)
        mock_clone.assert_called_once_with(
            "https://github.com/owner/repo.git",
            expected_root / "repo",
            branch="feature-branch",
            env={"GIT_TERMINAL_PROMPT": "0"},
        )
        mock_set_local.assert_called()

    def test_idempotent_when_repo_exists(
        self, patch_pr_workspace_root: Path
    ) -> None:
        ws_root = patch_pr_workspace_root
        ws_dir = ws_root / "owner" / "repo" / "42"
        ws_dir.mkdir(parents=True)
        (ws_dir / "repo").mkdir(parents=True)

        bot = MagicMock()
        with (
            patch("cai.github.repo.get_pr_meta") as mock_get_pr_meta,
            patch("cai.github.repo.clone") as mock_clone,
            patch("cai.github.repo.set_local") as mock_set_local,
        ):
            mock_get_pr_meta.return_value = (
                "Title",
                "Body",
                "feature",
                "main",
            )
            result = prepare_pr_workspace(bot, "owner/repo", 42)

        assert result.root == ws_dir
        mock_get_pr_meta.assert_called_once()  # PR meta is always fetched.
        mock_clone.assert_not_called()
        mock_set_local.assert_called()
