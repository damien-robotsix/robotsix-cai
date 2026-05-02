"""Tests for traced_agent_run in cai.log.observability.

traced_agent_run() wraps a pydantic-ai agent.run() call inside a
Langfuse ``start_as_current_observation`` span when Langfuse has been
initialised, and falls through to a plain ``await agent.run()`` when it
has not.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("genai_prices")

from cai.log.observability import traced_agent_run


class TestTracedAgentRunNotInitialized:
    """traced_agent_run() when Langfuse has **not** been initialised
    (``_initialized`` is ``False``, the default).
    """

    def test_calls_agent_run_directly(self):
        """agent.run(prompt) is called without any Langfuse wrapping."""
        async def _run():
            agent = AsyncMock()
            result = await traced_agent_run("test", agent, "hello")
            agent.run.assert_awaited_once_with("hello")
            assert result is agent.run.return_value

        asyncio.run(_run())

    def test_forwards_all_kwargs(self):
        """All extra keyword arguments reach agent.run()."""
        async def _run():
            agent = AsyncMock()
            await traced_agent_run("test", agent, "hi", deps="d", usage_limits="ul")
            agent.run.assert_awaited_once_with("hi", deps="d", usage_limits="ul")

        asyncio.run(_run())

    def test_returns_agent_result(self):
        """The return value from agent.run() is passed through unchanged."""
        async def _run():
            agent = AsyncMock()
            agent.run.return_value = {"output": "done"}
            result = await traced_agent_run("test", agent, "hello")
            assert result == {"output": "done"}

        asyncio.run(_run())

    def test_no_langfuse_import_error_on_uninitialized_path(self):
        """The uninitialized path does not attempt to import langfuse or
        create a span — if it did, the patch below would explode."""
        async def _run():
            agent = AsyncMock()
            with patch("langfuse.get_client", side_effect=ImportError("not needed")):
                # Even though get_client would raise, we never reach that code
                # because _initialized is False.
                result = await traced_agent_run("test", agent, "hello")
                assert result is agent.run.return_value

        asyncio.run(_run())


class TestTracedAgentRunInitialized:
    """traced_agent_run() when Langfuse **is** initialised
    (``_initialized`` is ``True``).
    """

    @staticmethod
    def _mock_langfuse_env() -> MagicMock:
        """Return a mock Langfuse client whose ``start_as_current_observation``
        returns a usable context manager."""
        mock_client = MagicMock()
        mock_client.start_as_current_observation.return_value = MagicMock()
        return mock_client

    def test_creates_langfuse_span_with_name_type_and_input(self):
        """agent.run() is wrapped in a ``start_as_current_observation`` span
        with the correct name, as_type, and input."""
        async def _run():
            agent = AsyncMock()
            mock_client = self._mock_langfuse_env()

            with (
                patch("cai.log.observability._initialized", True),
                patch("langfuse.get_client", return_value=mock_client),
            ):
                result = await traced_agent_run("explore", agent, "investigate", deps="x")

            mock_client.start_as_current_observation.assert_called_once_with(
                name="explore",
                as_type="span",
                input="investigate",
            )
            agent.run.assert_awaited_once_with("investigate", deps="x")
            assert result is agent.run.return_value

        asyncio.run(_run())

    def test_returns_agent_result(self):
        """The return value from agent.run() is propagated out of the span."""
        async def _run():
            agent = AsyncMock()
            agent.run.return_value = {"completed": True}
            mock_client = self._mock_langfuse_env()

            with (
                patch("cai.log.observability._initialized", True),
                patch("langfuse.get_client", return_value=mock_client),
            ):
                result = await traced_agent_run("test", agent, "prompt")

            assert result == {"completed": True}

        asyncio.run(_run())
