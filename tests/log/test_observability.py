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


class TestIsLangfuseInitialized:
    """Unit tests for the ``_is_langfuse_initialized()`` helper.

    The helper looks up the ``_initialized`` flag from the module object
    registered in ``sys.modules`` at call time, which is the mechanism
    that makes ``traced_agent_run()`` survive module reload.
    """

    def test_true_when_initialized_is_true(self) -> None:
        """Returns ``True`` when ``_initialized`` is truthy."""
        from cai.log.observability import _is_langfuse_initialized

        with patch("cai.log.observability._initialized", True):
            assert _is_langfuse_initialized() is True

    def test_false_when_initialized_is_false(self) -> None:
        """Returns ``False`` when ``_initialized`` is ``False``."""
        from cai.log.observability import _is_langfuse_initialized

        with patch("cai.log.observability._initialized", False):
            assert _is_langfuse_initialized() is False

    def test_false_when_module_not_in_sys_modules(self) -> None:
        """Returns ``False`` when the module isn't in ``sys.modules`` at all."""
        import sys

        from cai.log.observability import _is_langfuse_initialized

        mod_name = "cai.log.observability"
        saved = sys.modules.pop(mod_name)
        try:
            assert _is_langfuse_initialized() is False
        finally:
            sys.modules[mod_name] = saved


class TestModuleReloadResilience:
    """``traced_agent_run()`` survives a delete/re-import cycle.

    When ``cai.log.observability`` is removed from ``sys.modules`` and
    re-imported (as happens in test suites that use ``importlib.reload``
    or manually delete cached modules), the old function object's
    ``__globals__`` still references the *old* module.  The fix is that
    ``_is_langfuse_initialized()`` looks up ``_initialized`` from
    ``sys.modules[__name__]`` at call time rather than from the
    closure-bound module dict, so it always sees the *current* module's
    flag.
    """

    def test_uninitialized_path_after_reload(self) -> None:
        """After a delete/re-import, ``traced_agent_run()`` correctly sees the
        **new** module's ``_initialized=False`` and takes the fast path,
        *even though* the function's ``__globals__`` still points to the old
        module where ``_initialized`` was set to ``True``."""
        import sys

        mod_name = "cai.log.observability"
        old_mod = sys.modules[mod_name]

        # Set _initialized=True on the OLD module (what __globals__ sees)
        old_mod._initialized = True

        # Delete from sys.modules and re-import — creates a NEW module object
        # whose _initialized is False (the module-level default).
        del sys.modules[mod_name]
        import cai.log.observability  # noqa: F811

        try:
            async def _run() -> None:
                agent = AsyncMock(spec=["run"])
                agent.run = AsyncMock(return_value="result")
                # If the old function read from __globals__ it would see True
                # and try to import Langfuse → ImportError.  The fix reads
                # from sys.modules, so it sees False and takes the fast path.
                with patch("langfuse.get_client", side_effect=ImportError("should not import")):
                    result = await traced_agent_run("test", agent, "hello")
                agent.run.assert_awaited_once_with("hello")
                assert result is agent.run.return_value

            asyncio.run(_run())
        finally:
            sys.modules[mod_name] = old_mod

    def test_patched_initialized_after_reload(self) -> None:
        """After a delete/re-import, patching the **new** module's
        ``_initialized`` to ``True`` is visible to ``traced_agent_run``,
        confirming the ``sys.modules`` lookup — not ``__globals__`` —
        controls the flag."""
        import sys

        mod_name = "cai.log.observability"
        old_mod = sys.modules[mod_name]

        # Set _initialized=True on the OLD module (to contrast with the patch
        # target on the new module)
        old_mod._initialized = True

        del sys.modules[mod_name]
        import cai.log.observability  # noqa: F811

        try:
            async def _run() -> None:
                agent = AsyncMock(spec=["run"])
                agent.run = AsyncMock(return_value="result")
                mock_client = MagicMock()
                mock_client.start_as_current_observation.return_value.__enter__ = AsyncMock()
                mock_client.start_as_current_observation.return_value.__exit__ = AsyncMock()

                with (
                    patch("cai.log.observability._initialized", True),
                    patch("langfuse.get_client", return_value=mock_client),
                ):
                    result = await traced_agent_run("explore", agent, "investigate", deps="x")

                # Confirm the Langfuse path was taken (initialized branch)
                mock_client.start_as_current_observation.assert_called_once_with(
                    name="explore",
                    as_type="span",
                    input="investigate",
                )
                agent.run.assert_awaited_once_with("investigate", deps="x")
                assert result is agent.run.return_value

            asyncio.run(_run())
        finally:
            sys.modules[mod_name] = old_mod


class TestTracedAgentRunSoftRetry:
    """When ``UsageLimitExceeded`` is raised, ``traced_agent_run`` bumps
    the ``request_limit`` by 50% and retries exactly once.
    """

    def test_retry_succeeds_on_second_attempt(self):
        """First call raises UsageLimitExceeded, second call succeeds → returns
        second-call output and the span carries soft_retry metadata."""
        from pydantic_ai.exceptions import UsageLimitExceeded
        from pydantic_ai.usage import UsageLimits

        async def _run():
            agent = MagicMock()
            # First call raises, second succeeds
            agent.run = AsyncMock(side_effect=[
                UsageLimitExceeded("request limit exceeded"),
                {"retry": "ok"},
            ])

            with patch("cai.log.observability._initialized", False):
                result = await traced_agent_run(
                    "explore", agent, "investigate",
                    usage_limits=UsageLimits(request_limit=30),
                )

            assert result == {"retry": "ok"}
            assert agent.run.await_count == 2
            # Second call had bumped limits
            _, kwargs2 = agent.run.await_args_list[1]
            assert kwargs2["usage_limits"].request_limit == 45

        asyncio.run(_run())

    def test_retry_bubbles_up_on_second_failure(self):
        """Both calls raise UsageLimitExceeded → re-raises so the workflow
        still fails loudly."""
        from pydantic_ai.exceptions import UsageLimitExceeded

        async def _run():
            agent = MagicMock()
            agent.run = AsyncMock(side_effect=UsageLimitExceeded("exhausted"))

            with patch("cai.log.observability._initialized", False):
                with pytest.raises(UsageLimitExceeded):
                    await traced_agent_run("test", agent, "prompt")

            assert agent.run.await_count == 2

        asyncio.run(_run())

    def test_no_usage_limits_retry_still_works(self):
        """When no usage_limits are passed, the retry still fires (just
        without bumping the limit)."""
        from pydantic_ai.exceptions import UsageLimitExceeded

        async def _run():
            agent = MagicMock()
            agent.run = AsyncMock(side_effect=[
                UsageLimitExceeded("exhausted"),
                {"ok": True},
            ])

            with patch("cai.log.observability._initialized", False):
                result = await traced_agent_run("test", agent, "prompt")

            assert result == {"ok": True}
            assert agent.run.await_count == 2

        asyncio.run(_run())

    def test_soft_retry_metadata_on_langfuse_span(self):
        """When Langfuse is initialized, the retry path sets ``soft_retry``
        metadata on the current span."""
        from pydantic_ai.exceptions import UsageLimitExceeded
        from pydantic_ai.usage import UsageLimits

        async def _run():
            agent = MagicMock()
            agent.run = AsyncMock(side_effect=[
                UsageLimitExceeded("request limit exceeded"),
                {"retry": "ok"},
            ])

            mock_client = MagicMock()
            mock_client.start_as_current_observation.return_value = MagicMock()

            with (
                patch("cai.log.observability._initialized", True),
                patch("langfuse.get_client", return_value=mock_client),
            ):
                result = await traced_agent_run(
                    "explore", agent, "prompt",
                    usage_limits=UsageLimits(request_limit=100),
                )

            assert result == {"retry": "ok"}
            mock_client.update_current_span.assert_called_once_with(
                metadata={"soft_retry": True}
            )

        asyncio.run(_run())
