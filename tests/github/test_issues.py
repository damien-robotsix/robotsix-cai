import json
import pytest
from unittest.mock import Mock

from cai.github.issues import pull, push


def _setup_push_test(tmp_path, mock_caibot_class, labels, number=None,
                     md_exists=True, state="open", assignees=None,
                     milestone=None):
    """Create JSON+MD files and wire up mocks for a push() call.

    Returns (json_path, mock_bot, mock_repo) so callers can assert on
    repo-level calls (create_issue / get_issue / edit).
    """
    mock_bot = Mock()
    mock_caibot_class.return_value = mock_bot
    mock_repo = Mock()
    mock_bot.repo.return_value = mock_repo
    mock_issue = Mock()
    mock_issue.number = 42
    mock_repo.create_issue.return_value = mock_issue
    mock_repo.get_issue.return_value = mock_issue

    meta = {
        "repo": "owner/repo",
        "title": "Test Issue",
        "labels": labels,
    }
    if number is not None:
        meta["number"] = number
    if state != "open":
        meta["state"] = state
    if assignees is not None:
        meta["assignees"] = assignees
    if milestone is not None:
        meta["milestone"] = milestone

    json_path = tmp_path / "new_issue.json"
    json_path.write_text(json.dumps(meta))

    if md_exists:
        md_path = tmp_path / "new_issue.md"
        md_path.write_text("Test Body")

    return json_path, mock_bot, mock_repo


def _assert_ensure_labels_called(mock_ensure_labels, mock_bot):
    mock_ensure_labels.assert_called_once()
    args, _ = mock_ensure_labels.call_args
    assert args[0] == mock_bot
    assert args[1] == "owner/repo"
    labels_passed = args[2]
    assert len(labels_passed) == 5
    assert labels_passed[0].name == "cai:raised"
    assert labels_passed[1].name == "cai:audit"
    assert labels_passed[2].name == "cai:pr-ready"
    assert labels_passed[3].name == "cai:failed"
    assert labels_passed[4].name == "cai:human-review"


# ---------------------------------------------------------------------------
# ensure_labels gating
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("trigger_label", [
    "cai:raised", "cai:audit", "cai:pr-ready", "cai:failed", "cai:human-review",
])
def test_push_ensure_labels_called_for_cai_labels(push_mocks, tmp_path, trigger_label):
    mock_caibot_class, mock_ensure_labels, mock_resolve = push_mocks
    mock_resolve.return_value = None
    json_path, mock_bot, _ = _setup_push_test(
        tmp_path, mock_caibot_class, [trigger_label],
    )
    push(mock_bot, json_path)
    _assert_ensure_labels_called(mock_ensure_labels, mock_bot)


def test_push_ensure_labels_not_called_for_non_cai_labels(push_mocks, tmp_path):
    mock_caibot_class, mock_ensure_labels, mock_resolve = push_mocks
    mock_resolve.return_value = None
    json_path, mock_bot, _ = _setup_push_test(
        tmp_path, mock_caibot_class, ["enhancement", "bug"],
    )
    push(mock_bot, json_path)
    mock_ensure_labels.assert_not_called()


def test_push_ensure_labels_called_when_mixed_labels(push_mocks, tmp_path):
    """If *any* label starts with 'cai:', ensure_labels is called."""
    mock_caibot_class, mock_ensure_labels, mock_resolve = push_mocks
    mock_resolve.return_value = None
    json_path, mock_bot, _ = _setup_push_test(
        tmp_path, mock_caibot_class, ["bug", "cai:raised"],
    )
    push(mock_bot, json_path)
    _assert_ensure_labels_called(mock_ensure_labels, mock_bot)


# ---------------------------------------------------------------------------
# creation path (number is None)
# ---------------------------------------------------------------------------


def test_push_creates_issue_when_number_absent(push_mocks, tmp_path):
    """When meta.number is missing, create_issue is called."""
    mock_caibot_class, mock_ensure_labels, mock_resolve = push_mocks
    mock_resolve.return_value = None
    json_path, mock_bot, mock_repo = _setup_push_test(
        tmp_path, mock_caibot_class, ["enhancement"],
    )

    result = push(mock_bot, json_path)

    mock_repo.create_issue.assert_called_once()
    call_kwargs = mock_repo.create_issue.call_args[1]
    assert call_kwargs["title"] == "Test Issue"
    assert call_kwargs["body"] == "Test Body"
    assert call_kwargs["labels"] == ["enhancement"]
    # number written back to JSON
    assert json.loads(json_path.read_text())["number"] == 42
    assert result.number == 42


def test_push_creates_issue_with_assignees(push_mocks, tmp_path):
    mock_caibot_class, mock_ensure_labels, mock_resolve = push_mocks
    mock_resolve.return_value = None
    json_path, mock_bot, mock_repo = _setup_push_test(
        tmp_path, mock_caibot_class, ["bug"],
        assignees=["alice"],
    )
    push(mock_bot, json_path)

    call_kwargs = mock_repo.create_issue.call_args[1]
    assert call_kwargs["assignees"] == ["alice"]


def test_push_creates_issue_with_milestone(push_mocks, tmp_path):
    mock_caibot_class, mock_ensure_labels, mock_resolve = push_mocks
    fake_milestone = Mock()
    mock_resolve.return_value = fake_milestone
    json_path, mock_bot, mock_repo = _setup_push_test(
        tmp_path, mock_caibot_class, ["bug"],
        milestone="v2.0",
    )
    push(mock_bot, json_path)

    mock_resolve.assert_called_once_with(mock_repo, "v2.0")
    call_kwargs = mock_repo.create_issue.call_args[1]
    assert call_kwargs["milestone"] is fake_milestone


def test_push_creates_closed_issue(push_mocks, tmp_path):
    """When state is 'closed' and number is None, the issue is created then
    immediately edited to closed."""
    mock_caibot_class, mock_ensure_labels, mock_resolve = push_mocks
    mock_resolve.return_value = None
    json_path, mock_bot, mock_repo = _setup_push_test(
        tmp_path, mock_caibot_class, ["enhancement"],
        state="closed",
    )

    result = push(mock_bot, json_path)

    # create_issue still called
    mock_repo.create_issue.assert_called_once()
    # then edited to closed
    result.edit.assert_called_once()
    edit_kwargs = result.edit.call_args[1]
    assert edit_kwargs["state"] == "closed"


# ---------------------------------------------------------------------------
# update path (number is set)
# ---------------------------------------------------------------------------


def test_push_updates_issue_when_number_present(push_mocks, tmp_path):
    """When meta.number is provided, get_issue + edit are used."""
    mock_caibot_class, mock_ensure_labels, mock_resolve = push_mocks
    mock_resolve.return_value = None
    json_path, mock_bot, mock_repo = _setup_push_test(
        tmp_path, mock_caibot_class, ["enhancement", "bug"],
        number=7,
    )

    result = push(mock_bot, json_path)

    mock_repo.get_issue.assert_called_once_with(7)
    mock_repo.create_issue.assert_not_called()
    result.edit.assert_called_once()
    edit_kwargs = result.edit.call_args[1]
    assert edit_kwargs["title"] == "Test Issue"
    assert edit_kwargs["body"] == "Test Body"
    assert edit_kwargs["state"] == "open"
    assert edit_kwargs["labels"] == ["enhancement", "bug"]


def test_push_update_passes_milestone(push_mocks, tmp_path):
    mock_caibot_class, mock_ensure_labels, mock_resolve = push_mocks
    fake_milestone = Mock()
    mock_resolve.return_value = fake_milestone
    json_path, mock_bot, mock_repo = _setup_push_test(
        tmp_path, mock_caibot_class, ["bug"],
        number=7, milestone="v2.0",
    )
    result = push(mock_bot, json_path)

    edit_kwargs = result.edit.call_args[1]
    assert edit_kwargs["milestone"] is fake_milestone


# ---------------------------------------------------------------------------
# error paths
# ---------------------------------------------------------------------------


def test_push_raises_when_md_file_missing(push_mocks, tmp_path):
    mock_caibot_class, mock_ensure_labels, mock_resolve = push_mocks
    json_path, mock_bot, _ = _setup_push_test(
        tmp_path, mock_caibot_class, ["bug"],
        md_exists=False,
    )
    with pytest.raises(FileNotFoundError, match="missing issue body file"):
        push(mock_bot, json_path)


# ---------------------------------------------------------------------------
# label boundary cases
# ---------------------------------------------------------------------------


def test_push_no_labels_does_not_call_ensure_labels(push_mocks, tmp_path):
    mock_caibot_class, mock_ensure_labels, mock_resolve = push_mocks
    mock_resolve.return_value = None
    json_path, mock_bot, _ = _setup_push_test(
        tmp_path, mock_caibot_class, [],
    )
    push(mock_bot, json_path)
    mock_ensure_labels.assert_not_called()


def test_push_ensure_labels_not_triggered_by_non_prefix_match(push_mocks, tmp_path):
    """Labels containing 'cai:' but not as a prefix should not trigger."""
    mock_caibot_class, mock_ensure_labels, mock_resolve = push_mocks
    mock_resolve.return_value = None
    json_path, mock_bot, _ = _setup_push_test(
        tmp_path, mock_caibot_class, ["not-cai:raised"],
    )
    push(mock_bot, json_path)
    mock_ensure_labels.assert_not_called()


# ---------------------------------------------------------------------------
# pull() tests
# ---------------------------------------------------------------------------


def _setup_pull_test(tmp_path, body="Issue body text", comments=None):
    """Create mocks for a pull() call and return (bot, directory, number)."""
    from datetime import datetime

    mock_bot = Mock()
    mock_repo = Mock()
    mock_bot.repo.return_value = mock_repo

    mock_issue = Mock()
    mock_issue.body = body
    mock_issue.title = "Test Issue"
    mock_issue.state = "open"
    mock_issue.state_reason = None
    mock_issue.labels = []
    mock_issue.assignees = []
    mock_issue.milestone = None
    mock_repo.get_issue.return_value = mock_issue

    if comments is None:
        mock_issue.get_comments.return_value = []
    else:
        mock_comments = []
        for author, created_at, body_text in comments:
            mc = Mock()
            mc.user.login = author
            mc.created_at = created_at
            mc.body = body_text
            mock_comments.append(mc)
        mock_issue.get_comments.return_value = mock_comments

    directory = tmp_path / "issues"
    return mock_bot, directory, 42


def test_pull_writes_body_without_comments_when_none(tmp_path):
    """pull() writes only the body when get_comments() returns an empty list."""
    mock_bot, directory, number = _setup_pull_test(tmp_path, body="Plain body")
    pull(mock_bot, "owner/repo", number, directory)

    md_path = directory / f"{number}.md"
    assert md_path.read_text() == "Plain body"


def test_pull_appends_comments_section(tmp_path):
    """pull() writes body + ## Issue Comments section when comments exist."""
    from datetime import datetime

    comments = [
        ("alice", datetime(2025, 1, 15, 10, 30), "First comment."),
        ("bob", datetime(2025, 1, 15, 11, 0), "Second comment\nwith two lines."),
    ]
    mock_bot, directory, number = _setup_pull_test(
        tmp_path, body="Main body", comments=comments
    )
    pull(mock_bot, "owner/repo", number, directory)

    md_path = directory / f"{number}.md"
    content = md_path.read_text()

    assert content.startswith("Main body")
    assert "## Issue Comments" in content
    assert "**@alice** (2025-01-15 10:30):" in content
    assert "First comment." in content
    assert "**@bob** (2025-01-15 11:00):" in content
    assert content.endswith("with two lines.")


def test_pull_single_comment(tmp_path):
    """pull() correctly formats the comments section with a single comment."""
    from datetime import datetime

    comments = [
        ("alice", datetime(2025, 1, 15, 10, 30), "Only comment."),
    ]
    mock_bot, directory, number = _setup_pull_test(
        tmp_path, body="Main body", comments=comments
    )
    pull(mock_bot, "owner/repo", number, directory)

    md_path = directory / f"{number}.md"
    content = md_path.read_text()

    assert content.startswith("Main body")
    assert "## Issue Comments" in content
    assert "**@alice** (2025-01-15 10:30):\nOnly comment." in content
    assert content.endswith("Only comment.")


def test_pull_none_body_without_comments(tmp_path):
    """pull() writes an empty string when body is None and there are no comments."""
    mock_bot, directory, number = _setup_pull_test(tmp_path, body=None)
    pull(mock_bot, "owner/repo", number, directory)

    md_path = directory / f"{number}.md"
    assert md_path.read_text() == ""


def test_pull_none_body_with_comments(tmp_path):
    """pull() handles a None body gracefully when comments exist."""
    from datetime import datetime

    comments = [
        ("alice", datetime(2025, 1, 15, 10, 30), "A comment."),
    ]
    mock_bot, directory, number = _setup_pull_test(
        tmp_path, body=None, comments=comments
    )
    pull(mock_bot, "owner/repo", number, directory)

    md_path = directory / f"{number}.md"
    content = md_path.read_text()

    assert "## Issue Comments" in content
    assert "**@alice**" in content
    assert "A comment." in content


def test_pull_returns_correct_paths(tmp_path):
    """pull() returns the (json_path, md_path) tuple pointing to the written files."""
    mock_bot, directory, number = _setup_pull_test(tmp_path, body="Body")
    json_path, md_path = pull(mock_bot, "owner/repo", number, directory)

    assert json_path == directory / f"{number}.json"
    assert md_path == directory / f"{number}.md"
    assert json_path.exists()
    assert md_path.exists()


@pytest.mark.parametrize("body,has_comments", [
    ("", False),
    ("", True),
    ("Hello world", False),
    ("Hello world", True),
])
def test_pull_does_not_raise_for_varied_inputs(tmp_path, body, has_comments):
    """pull() should not raise for any combination of body content and comment presence."""
    from datetime import datetime

    comments = None
    if has_comments:
        comments = [("user", datetime(2025, 1, 1, 12, 0), "A comment.")]

    mock_bot, directory, number = _setup_pull_test(
        tmp_path, body=body, comments=comments
    )
    pull(mock_bot, "owner/repo", number, directory)
    md_path = directory / f"{number}.md"
    assert md_path.exists()
