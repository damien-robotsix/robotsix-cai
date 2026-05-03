import asyncio
from unittest.mock import MagicMock, patch

import pytest

from cai.agents.issue_tool import RAISE_ISSUE_TOOL, raise_issue


def _run(coro):
    return asyncio.run(coro)


def _make_push_capturing_labels(mock_issue):
    """Build a push side-effect that snapshots the issue.json labels
    while the tempdir still exists, then returns ``mock_issue``."""
    import json
    captured: dict = {}

    def fake_push(bot, json_path):
        captured["labels"] = json.loads(json_path.read_text())["labels"]
        return mock_issue

    return fake_push, captured


# ---------------------------------------------------------------------------
# IssueMeta construction
# ---------------------------------------------------------------------------


def test_raise_issue_uses_default_labels_when_none_provided():
    """When labels is omitted, the default ``["cai:human-review"]`` is used,
    plus the ``cai:agent-raised`` provenance tag is auto-appended."""
    with (
        patch("cai.agents.issue_tool.CaiBot") as mock_caibot_class,
        patch("cai.agents.issue_tool.push") as mock_push,
    ):
        mock_bot = MagicMock()
        mock_caibot_class.return_value = mock_bot
        mock_issue = MagicMock()
        mock_issue.number = 99
        mock_issue.title = "Test Title"
        mock_issue.html_url = "https://github.com/owner/repo/issues/99"
        fake_push, captured = _make_push_capturing_labels(mock_issue)
        mock_push.side_effect = fake_push

        result = _run(raise_issue(
            ctx=None,
            repo="owner/repo",
            title="Test Title",
            body="Test body",
        ))

    assert captured["labels"] == ["cai:human-review", "cai:agent-raised"]
    assert "99" in result
    assert "https://github.com/owner/repo/issues/99" in result


def test_raise_issue_appends_agent_raised_to_custom_labels():
    """The ``cai:agent-raised`` provenance tag is auto-appended to any
    custom labels the agent passes."""
    with (
        patch("cai.agents.issue_tool.CaiBot") as mock_caibot_class,
        patch("cai.agents.issue_tool.push") as mock_push,
    ):
        mock_bot = MagicMock()
        mock_caibot_class.return_value = mock_bot
        mock_issue = MagicMock()
        mock_issue.number = 7
        mock_issue.title = "X"
        mock_issue.html_url = "https://github.com/owner/repo/issues/7"
        fake_push, captured = _make_push_capturing_labels(mock_issue)
        mock_push.side_effect = fake_push

        _run(raise_issue(
            ctx=None,
            repo="owner/repo",
            title="X",
            body="Body",
            labels=["bug", "cai:raised"],
        ))

    assert captured["labels"] == ["bug", "cai:raised", "cai:agent-raised"]


def test_raise_issue_does_not_duplicate_agent_raised():
    """If the caller already includes ``cai:agent-raised``, it stays
    once — no duplicates."""
    with (
        patch("cai.agents.issue_tool.CaiBot") as mock_caibot_class,
        patch("cai.agents.issue_tool.push") as mock_push,
    ):
        mock_bot = MagicMock()
        mock_caibot_class.return_value = mock_bot
        mock_issue = MagicMock()
        mock_issue.number = 1
        mock_issue.title = "X"
        mock_issue.html_url = "u"
        fake_push, captured = _make_push_capturing_labels(mock_issue)
        mock_push.side_effect = fake_push

        _run(raise_issue(
            ctx=None,
            repo="owner/repo",
            title="X",
            body="b",
            labels=["cai:agent-raised", "bug"],
        ))

    assert captured["labels"].count("cai:agent-raised") == 1


def test_raise_issue_uses_custom_labels():
    """When labels are provided, they are passed through instead of the default."""
    with (
        patch("cai.agents.issue_tool.CaiBot") as mock_caibot_class,
        patch("cai.agents.issue_tool.push") as mock_push,
    ):
        mock_bot = MagicMock()
        mock_caibot_class.return_value = mock_bot
        mock_issue = MagicMock()
        mock_issue.number = 7
        mock_issue.title = "Custom Label Issue"
        mock_issue.html_url = "https://github.com/owner/repo/issues/7"
        mock_push.return_value = mock_issue

        result = _run(raise_issue(
            ctx=None,
            repo="owner/repo",
            title="Custom Label Issue",
            body="Body",
            labels=["bug", "cai:raised"],
        ))

    assert "7" in result
    assert "Custom Label Issue" in result
    assert "https://github.com/owner/repo/issues/7" in result


# ---------------------------------------------------------------------------
# Temp file writing
# ---------------------------------------------------------------------------


def test_raise_issue_writes_json_and_md_to_tempdir():
    """The tool writes issue.json (IssueMeta) and issue.md (body) in a temp dir."""
    with (
        patch("cai.agents.issue_tool.CaiBot") as mock_caibot_class,
        patch("cai.agents.issue_tool.push") as mock_push,
    ):
        mock_bot = MagicMock()
        mock_caibot_class.return_value = mock_bot
        mock_issue = MagicMock()
        mock_issue.number = 42
        mock_issue.title = "File Write Test"
        mock_issue.html_url = "https://github.com/owner/repo/issues/42"
        mock_push.return_value = mock_issue

        _run(raise_issue(
            ctx=None,
            repo="owner/repo",
            title="File Write Test",
            body="Body content here.",
            labels=["enhancement"],
        ))

        # push was called with (mock_bot, json_path)
        call_args = mock_push.call_args[0]
        assert call_args[0] is mock_bot
        assert call_args[1].name == "issue.json"


# ---------------------------------------------------------------------------
# Return value
# ---------------------------------------------------------------------------


def test_raise_issue_return_format():
    """The confirmation string includes issue number, title, and URL."""
    with (
        patch("cai.agents.issue_tool.CaiBot") as mock_caibot_class,
        patch("cai.agents.issue_tool.push") as mock_push,
    ):
        mock_bot = MagicMock()
        mock_caibot_class.return_value = mock_bot
        mock_issue = MagicMock()
        mock_issue.number = 123
        mock_issue.title = "A Blocking Bug"
        mock_issue.html_url = "https://github.com/org/repo/issues/123"
        mock_push.return_value = mock_issue

        result = _run(raise_issue(
            ctx=None,
            repo="org/repo",
            title="A Blocking Bug",
            body="Something went wrong.",
        ))

    expected = (
        "Issue created: #123 — A Blocking Bug\n"
        "https://github.com/org/repo/issues/123"
    )
    assert result == expected


# ---------------------------------------------------------------------------
# Tool constant
# ---------------------------------------------------------------------------


def test_raise_issue_tool_constant_exists():
    """RAISE_ISSUE_TOOL is a Tool wrapping the raise_issue function."""
    from pydantic_ai import Tool

    assert isinstance(RAISE_ISSUE_TOOL, Tool)
    assert RAISE_ISSUE_TOOL.name == "raise_issue"


# ---------------------------------------------------------------------------
# Registration in TOOL_FACTORIES
# ---------------------------------------------------------------------------


def test_raise_issue_registered_in_tool_factories():
    """The tool is registered under the key 'raise_issue' in loader.py."""
    from cai.agents.loader import TOOL_FACTORIES

    assert "raise_issue" in TOOL_FACTORIES
    assert TOOL_FACTORIES["raise_issue"] == "cai.agents.issue_tool:RAISE_ISSUE_TOOL"


def test_import_factory_resolves_raise_issue():
    """The factory target string imports and returns the RAISE_ISSUE_TOOL."""
    from cai.agents.loader import _import_factory, TOOL_FACTORIES

    tool = _import_factory(TOOL_FACTORIES["raise_issue"])
    assert tool is RAISE_ISSUE_TOOL
