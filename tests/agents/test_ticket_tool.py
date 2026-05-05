import asyncio
from unittest.mock import MagicMock, patch

import pytest

from cai.agents.ticket_tool import RAISE_TICKET_TOOL, raise_ticket


def _run(coro):
    return asyncio.run(coro)


def _bot_with_project():
    bot = MagicMock()
    bot.project_owner = "damien-robotsix"
    bot.project_number = 7
    bot.project_default_repo = "damien-robotsix/robotsix-cai"
    return bot


def _bot_without_project():
    bot = MagicMock()
    bot.project_owner = None
    bot.project_number = None
    bot.project_default_repo = "damien-robotsix/robotsix-cai"
    return bot


# ---------------------------------------------------------------------------
# Project-enabled path
# ---------------------------------------------------------------------------


class TestProjectPath:
    def test_creates_draft_with_defaults(self):
        with (
            patch("cai.agents.ticket_tool.CaiBot") as mock_caibot,
            patch("cai.agents.ticket_tool.create_draft_ticket") as mock_create,
        ):
            mock_caibot.return_value = _bot_with_project()
            mock_create.return_value = "PVTI_NEW"

            result = _run(raise_ticket(
                ctx=None,
                title="Refactor X",
                body="Body",
                type="code-change",
            ))

            mock_create.assert_called_once()
            kwargs = mock_create.call_args.kwargs
            assert kwargs["title"] == "Refactor X"
            assert kwargs["body"] == "Body"
            assert kwargs["type"] == "code-change"
            assert kwargs["status"] == "Backlog"
            assert "Ticket created" in result
            assert "PVTI_NEW" in result
            assert "Type=code-change" in result
            assert "Status=Backlog" in result

    def test_status_ready_passed_through(self):
        with (
            patch("cai.agents.ticket_tool.CaiBot") as mock_caibot,
            patch("cai.agents.ticket_tool.create_draft_ticket") as mock_create,
        ):
            mock_caibot.return_value = _bot_with_project()
            mock_create.return_value = "PVTI_R"

            _run(raise_ticket(
                ctx=None,
                title="t",
                body="b",
                type="analysis",
                status="Ready",
            ))

            assert mock_create.call_args.kwargs["status"] == "Ready"


# ---------------------------------------------------------------------------
# Fallback path: no project configured
# ---------------------------------------------------------------------------


class TestFallbackPath:
    def test_falls_back_to_issue_creation_when_project_missing(self):
        with (
            patch("cai.agents.ticket_tool.CaiBot") as mock_caibot,
            patch("cai.agents.ticket_tool.push") as mock_push,
            patch("cai.agents.ticket_tool.create_draft_ticket") as mock_create,
        ):
            mock_caibot.return_value = _bot_without_project()
            mock_issue = MagicMock()
            mock_issue.number = 12
            mock_issue.title = "x"
            mock_issue.html_url = "https://example/12"
            mock_push.return_value = mock_issue

            result = _run(raise_ticket(
                ctx=None,
                title="x",
                body="b",
                type="analysis",
            ))

            mock_create.assert_not_called()
            mock_push.assert_called_once()
            assert "fallback" in result
            assert "#12" in result

    def test_fallback_passes_type_label(self):
        """Fallback path encodes type via cai:type:<type> label so a later
        migration can backfill tickets from old issues."""
        import json

        captured: dict = {}

        def fake_push(bot, json_path):
            captured["labels"] = json.loads(json_path.read_text())["labels"]
            mock_issue = MagicMock()
            mock_issue.number = 1
            mock_issue.title = "x"
            mock_issue.html_url = "u"
            return mock_issue

        with (
            patch("cai.agents.ticket_tool.CaiBot") as mock_caibot,
            patch("cai.agents.ticket_tool.push", side_effect=fake_push),
        ):
            mock_caibot.return_value = _bot_without_project()
            _run(raise_ticket(
                ctx=None, title="x", body="b", type="analysis", status="Ready",
            ))

        assert "cai:type:analysis" in captured["labels"]
        assert "cai:raised" in captured["labels"]
        assert "cai:agent-raised" in captured["labels"]

    def test_fallback_status_backlog_uses_human_review(self):
        import json
        captured: dict = {}

        def fake_push(bot, json_path):
            captured["labels"] = json.loads(json_path.read_text())["labels"]
            mock_issue = MagicMock()
            mock_issue.number = 1
            mock_issue.title = "x"
            mock_issue.html_url = "u"
            return mock_issue

        with (
            patch("cai.agents.ticket_tool.CaiBot") as mock_caibot,
            patch("cai.agents.ticket_tool.push", side_effect=fake_push),
        ):
            mock_caibot.return_value = _bot_without_project()
            _run(raise_ticket(
                ctx=None, title="x", body="b", type="code-change",
            ))

        assert "cai:human-review" in captured["labels"]
        assert "cai:raised" not in captured["labels"]

    def test_fallback_raises_without_target_repo(self):
        with patch("cai.agents.ticket_tool.CaiBot") as mock_caibot:
            bot = _bot_without_project()
            bot.project_default_repo = None
            mock_caibot.return_value = bot

            with pytest.raises(RuntimeError, match="no fallback repo"):
                _run(raise_ticket(
                    ctx=None, title="x", body="b", type="analysis",
                ))


# ---------------------------------------------------------------------------
# Tool constant + registration
# ---------------------------------------------------------------------------


def test_tool_constant_exists():
    from pydantic_ai import Tool
    assert isinstance(RAISE_TICKET_TOOL, Tool)
    assert RAISE_TICKET_TOOL.name == "raise_ticket"


def test_registered_in_tool_factories():
    from cai.agents.loader import TOOL_FACTORIES
    assert "raise_ticket" in TOOL_FACTORIES
    assert TOOL_FACTORIES["raise_ticket"] == "cai.agents.ticket_tool:RAISE_TICKET_TOOL"
    assert "raise_issue" not in TOOL_FACTORIES


def test_import_factory_resolves_raise_ticket():
    from cai.agents.loader import _import_factory, TOOL_FACTORIES
    tool = _import_factory(TOOL_FACTORIES["raise_ticket"])
    assert tool is RAISE_TICKET_TOOL
