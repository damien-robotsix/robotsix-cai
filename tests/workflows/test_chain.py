"""Tests for ``cai.workflows.chain`` — the ``cai-chain-sub-issue`` CLI.

Covers the ``orchestrate()`` business logic and the ``main()`` CLI entry
point, with all external GitHub API calls mocked via ``unittest.mock``.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cai.workflows.chain import main, orchestrate


# ── orchestrate() ─────────────────────────────────────────────────────────


def test_orchestrate_no_parent():
    """When the closed issue has no parent, returns None."""
    bot = MagicMock()
    with patch("cai.workflows.chain.get_parent_issue", return_value=None):
        result = orchestrate(bot, "owner/repo", 42)
    assert result is None


def test_orchestrate_no_siblings():
    """When the parent has no sub-issues, returns None."""
    bot = MagicMock()
    with patch("cai.workflows.chain.get_parent_issue", return_value=1):
        with patch("cai.workflows.chain.list_sub_issues", return_value=[]):
            result = orchestrate(bot, "owner/repo", 42)
    assert result is None


def test_orchestrate_only_closed_siblings():
    """When all siblings are closed, returns None."""
    bot = MagicMock()
    siblings = [
        {"number": 10, "title": "sub-1", "state": "closed", "state_reason": "completed"},
        {"number": 20, "title": "sub-2", "state": "closed", "state_reason": "completed"},
    ]
    with patch("cai.workflows.chain.get_parent_issue", return_value=1):
        with patch("cai.workflows.chain.list_sub_issues", return_value=siblings):
            result = orchestrate(bot, "owner/repo", 20)
    assert result is None


def test_orchestrate_open_sibling_before_closed():
    """When the only open sibling is numbered *before* the closed issue, returns None."""
    bot = MagicMock()
    siblings = [
        {"number": 10, "title": "sub-early", "state": "open", "state_reason": None},
        {"number": 20, "title": "sub-closed", "state": "closed", "state_reason": "completed"},
    ]
    with patch("cai.workflows.chain.get_parent_issue", return_value=1):
        with patch("cai.workflows.chain.list_sub_issues", return_value=siblings):
            result = orchestrate(bot, "owner/repo", 20)
    assert result is None


def test_orchestrate_open_sibling_after_closed_applies_label():
    """When an open sibling follows the closed one, cai:raised is applied and a summary is returned."""
    bot = MagicMock()
    siblings = [
        {"number": 10, "title": "sub-done", "state": "closed", "state_reason": "completed"},
        {"number": 20, "title": "sub-ready", "state": "open", "state_reason": None},
    ]
    with (
        patch("cai.workflows.chain.get_parent_issue", return_value=1),
        patch("cai.workflows.chain.list_sub_issues", return_value=siblings),
        patch("cai.workflows.chain.set_label") as mock_set_label,
    ):
        result = orchestrate(bot, "owner/repo", 10)

    assert result == "applied cai:raised to owner/repo#20"
    mock_set_label.assert_called_once_with(bot, "owner/repo", 20, "cai:raised", present=True)


def test_orchestrate_multiple_after_closed_chooses_lowest():
    """When multiple open siblings follow, the lowest-numbered one gets the label."""
    bot = MagicMock()
    siblings = [
        {"number": 5, "title": "first", "state": "closed", "state_reason": "completed"},
        {"number": 10, "title": "lowest-open", "state": "open", "state_reason": None},
        {"number": 15, "title": "middle-open", "state": "open", "state_reason": None},
        {"number": 20, "title": "highest-open", "state": "open", "state_reason": None},
    ]
    with (
        patch("cai.workflows.chain.get_parent_issue", return_value=1),
        patch("cai.workflows.chain.list_sub_issues", return_value=siblings),
        patch("cai.workflows.chain.set_label") as mock_set_label,
    ):
        result = orchestrate(bot, "owner/repo", 5)

    assert result == "applied cai:raised to owner/repo#10"
    mock_set_label.assert_called_once_with(bot, "owner/repo", 10, "cai:raised", present=True)


def test_orchestrate_sorts_by_number():
    """Siblings are sorted by number regardless of input order."""
    bot = MagicMock()
    # Unsorted input — sibling 12 comes before 9 in the list
    siblings = [
        {"number": 12, "title": "sub-c", "state": "open", "state_reason": None},
        {"number": 9, "title": "sub-a", "state": "open", "state_reason": None},
        {"number": 10, "title": "sub-b", "state": "open", "state_reason": None},
    ]
    with (
        patch("cai.workflows.chain.get_parent_issue", return_value=1),
        patch("cai.workflows.chain.list_sub_issues", return_value=siblings),
        patch("cai.workflows.chain.set_label") as mock_set_label,
    ):
        result = orchestrate(bot, "owner/repo", 8)

    # After sorting by number: 9, 10, 12. 9 is the first > 8.
    assert result == "applied cai:raised to owner/repo#9"
    mock_set_label.assert_called_once_with(bot, "owner/repo", 9, "cai:raised", present=True)


def test_orchestrate_closed_number_matches_open_sibling():
    """If closed_number matches an open sibling number, the next one is chosen."""
    bot = MagicMock()
    siblings = [
        {"number": 10, "title": "just-closed", "state": "closed", "state_reason": "completed"},
        {"number": 20, "title": "next-up", "state": "open", "state_reason": None},
    ]
    with (
        patch("cai.workflows.chain.get_parent_issue", return_value=1),
        patch("cai.workflows.chain.list_sub_issues", return_value=siblings),
        patch("cai.workflows.chain.set_label") as mock_set_label,
    ):
        result = orchestrate(bot, "owner/repo", 10)

    assert result == "applied cai:raised to owner/repo#20"
    mock_set_label.assert_called_once_with(bot, "owner/repo", 20, "cai:raised", present=True)


# ── main() CLI ────────────────────────────────────────────────────────────


@patch("sys.argv", ["cai-chain-sub-issue", "owner/repo#42"])
def test_main_valid_ref():
    """A valid ref is parsed and orchestrate() is called."""
    with (
        patch("cai.workflows.chain.parse_ref_and_bot", return_value=(MagicMock(), "owner/repo", 42)) as mock_parse,
        patch("cai.workflows.chain.orchestrate", return_value="applied cai:raised to owner/repo#43") as mock_orch,
        pytest.raises(SystemExit) as exc,
    ):
        main()

    assert exc.value.code == 0
    mock_parse.assert_called_once_with(
        "cai-chain-sub-issue",
        "When a sub-issue is closed on GitHub, locate its parent, "
        "find the next open sibling, and apply cai:raised so the "
        "cai-solve workflow picks it up automatically.",
    )
    mock_orch.assert_called_once()
    args = mock_orch.call_args
    assert isinstance(args[0][0], MagicMock)  # CaiBot instance
    assert args[0][1] == "owner/repo"
    assert args[0][2] == 42


@patch("sys.argv", ["cai-chain-sub-issue", "owner/repo#42"])
def test_main_prints_summary(capsys):
    """When orchestrate() returns a summary, it is printed to stdout."""
    with (
        patch("cai.workflows.chain.parse_ref_and_bot", return_value=(MagicMock(), "owner/repo", 42)),
        patch("cai.workflows.chain.orchestrate", return_value="applied cai:raised to owner/repo#43"),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "applied cai:raised to owner/repo#43"


@patch("sys.argv", ["cai-chain-sub-issue", "owner/repo#42"])
def test_main_no_next_sub_issue(capsys):
    """When orchestrate() returns None, 'no next sub-issue' is printed."""
    with (
        patch("cai.workflows.chain.parse_ref_and_bot", return_value=(MagicMock(), "owner/repo", 42)),
        patch("cai.workflows.chain.orchestrate", return_value=None),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "no next sub-issue"


@patch("sys.argv", ["cai-chain-sub-issue", "not-a-valid-ref"])
def test_main_invalid_ref():
    """An unparsable ref exits with an error message to stderr."""
    with (
        patch("cai.workflows.chain.parse_ref_and_bot", side_effect=SystemExit(2)),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    assert exc.value.code == 2  # argparse error exit code


@patch("sys.argv", ["cai-chain-sub-issue", "owner/repo#42"])
def test_main_always_exits_zero(capsys):
    """main() always exits with code 0, even when there is no next sub-issue."""
    with (
        patch("cai.workflows.chain.parse_ref_and_bot", return_value=(MagicMock(), "owner/repo", 42)),
        patch("cai.workflows.chain.orchestrate", return_value=None),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "no next sub-issue"


@patch("sys.argv", ["cai-chain-sub-issue", "owner/repo#42"])
def test_main_exits_zero_on_success():
    """main() exits with code 0 on success."""
    with (
        patch("cai.workflows.chain.parse_ref_and_bot", return_value=(MagicMock(), "owner/repo", 42)),
        patch("cai.workflows.chain.orchestrate", return_value="applied cai:raised to owner/repo#43"),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    assert exc.value.code == 0
