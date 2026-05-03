"""Tests for PostEditVerificationGuardrail.

This guardrail blocks excessive ``spike_run`` verification calls when no
edits have been made. After 3 consecutive ``spike_run`` calls without an
intervening file-modifying tool, it returns a warning; on the 4th call
it raises ``ModelRetry``.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic_ai.exceptions import ModelRetry

from cai.agents.loader import PostEditVerificationGuardrail


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _Call:
    """Minimal mock for the ``call`` object expected by wrap_tool_execute."""
    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name


def _handler(result: str = "handled") -> AsyncMock:
    return AsyncMock(return_value=result)


# ---------------------------------------------------------------------------
# Pass-through behaviour (first two calls)
# ---------------------------------------------------------------------------


class TestPostEditVerificationGuardrail:
    """Tests for PostEditVerificationGuardrail's wrap_tool_execute logic."""

    def test_first_spike_passes_through(self) -> None:
        """The first consecutive spike_run call is executed normally."""
        guardrail = PostEditVerificationGuardrail()
        handler = _handler()
        call = _Call("spike_run")

        result = asyncio.run(guardrail.wrap_tool_execute(
            None, call=call, tool_def=None, args={}, handler=handler
        ))

        assert result == "handled"
        assert handler.call_count == 1
        assert guardrail._spike_count == 1
        assert guardrail._warned is False

    def test_second_spike_passes_through(self) -> None:
        """The second consecutive spike_run call is also executed normally."""
        guardrail = PostEditVerificationGuardrail()
        guardrail._spike_count = 1
        handler = _handler()
        call = _Call("spike_run")

        result = asyncio.run(guardrail.wrap_tool_execute(
            None, call=call, tool_def=None, args={}, handler=handler
        ))

        assert result == "handled"
        assert handler.call_count == 1
        assert guardrail._spike_count == 2
        assert guardrail._warned is False

    # ------------------------------------------------------------------
    # Warning on threshold breach
    # ------------------------------------------------------------------

    def test_third_spike_returns_warning(self) -> None:
        """The third consecutive spike_run returns a warning and skips the handler."""
        guardrail = PostEditVerificationGuardrail()
        guardrail._spike_count = 2
        handler = _handler()
        call = _Call("spike_run")

        result = asyncio.run(guardrail.wrap_tool_execute(
            None, call=call, tool_def=None, args={}, handler=handler
        ))

        assert isinstance(result, str)
        assert result.startswith("Warning:")
        assert "spike_run" in result
        assert "ImplementOutput" in result
        assert handler.call_count == 0  # handler was NOT called
        assert guardrail._warned is True

    def test_warning_message_says_do_not_call_spike_run_again(self) -> None:
        """The warning prompt tells the agent not to call spike_run again."""
        guardrail = PostEditVerificationGuardrail()
        guardrail._spike_count = 2
        call = _Call("spike_run")

        result = asyncio.run(guardrail.wrap_tool_execute(
            None, call=call, tool_def=None, args={}, handler=MagicMock()
        ))

        assert "do not call spike_run again" in str(result).lower()

    # ------------------------------------------------------------------
    # ModelRetry escalation
    # ------------------------------------------------------------------

    def test_fourth_spike_raises_model_retry(self) -> None:
        """After the warning has been issued, the next spike_run raises ModelRetry."""
        guardrail = PostEditVerificationGuardrail()
        guardrail._spike_count = 3
        guardrail._warned = True
        handler = _handler()
        call = _Call("spike_run")

        with pytest.raises(ModelRetry) as excinfo:
            asyncio.run(guardrail.wrap_tool_execute(
                None, call=call, tool_def=None, args={}, handler=handler
            ))

        msg = str(excinfo.value)
        assert "spike_run" in msg
        assert "ImplementOutput" in msg
        assert "do not call spike_run again" in msg.lower()
        assert handler.call_count == 0

    def test_warning_flag_persists_for_escalation(self) -> None:
        """``_warned`` remains True after warning, ensuring next call escalates."""
        guardrail = PostEditVerificationGuardrail()
        guardrail._spike_count = 2
        call = _Call("spike_run")

        asyncio.run(guardrail.wrap_tool_execute(
            None, call=call, tool_def=None, args={}, handler=MagicMock()
        ))
        assert guardrail._warned is True

        # Next call should raise
        with pytest.raises(ModelRetry):
            asyncio.run(guardrail.wrap_tool_execute(
                None, call=call, tool_def=None, args={}, handler=MagicMock()
            ))

    # ------------------------------------------------------------------
    # File-modifying tool resets
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("tool_name", [
        "write_file",
        "edit_file",
        "move_file",
        "delete_file",
        "batch_move",
        "batch_delete",
        "block_edit",
    ])
    def test_file_modifying_tool_resets_counter(self, tool_name: str) -> None:
        """Every file-modifying tool resets the spike counter and warning flag."""
        guardrail = PostEditVerificationGuardrail()
        guardrail._spike_count = 3
        guardrail._warned = True
        handler = _handler("edited")

        result = asyncio.run(guardrail.wrap_tool_execute(
            None, call=_Call(tool_name), tool_def=None, args={}, handler=handler
        ))

        assert result == "edited"
        assert guardrail._spike_count == 0
        assert guardrail._warned is False
        assert handler.call_count == 1

    def test_spike_after_edit_resumes_counting(self) -> None:
        """After an edit resets the counter, spike_run counting starts fresh."""
        guardrail = PostEditVerificationGuardrail()
        guardrail._spike_count = 2
        handler = _handler("ok")

        # An edit_file call resets
        asyncio.run(guardrail.wrap_tool_execute(
            None, call=_Call("edit_file"), tool_def=None, args={}, handler=handler
        ))
        assert guardrail._spike_count == 0

        # Two new spike calls should pass through
        asyncio.run(guardrail.wrap_tool_execute(
            None, call=_Call("spike_run"), tool_def=None, args={}, handler=handler
        ))
        assert guardrail._spike_count == 1
        assert guardrail._warned is False

        asyncio.run(guardrail.wrap_tool_execute(
            None, call=_Call("spike_run"), tool_def=None, args={}, handler=handler
        ))
        assert guardrail._spike_count == 2
        assert guardrail._warned is False

        # Third would warn, fourth would raise ModelRetry
        result = asyncio.run(guardrail.wrap_tool_execute(
            None, call=_Call("spike_run"), tool_def=None, args={}, handler=handler
        ))
        assert isinstance(result, str) and result.startswith("Warning:")
        assert guardrail._warned is True

    # ------------------------------------------------------------------
    # Non-target tools pass through
    # ------------------------------------------------------------------

    def test_non_spike_tool_passes_through(self) -> None:
        """A tool that is neither spike_run nor file-modifying passes through."""
        guardrail = PostEditVerificationGuardrail()
        guardrail._spike_count = 2
        handler = _handler("done")
        call = _Call("read_file")

        result = asyncio.run(guardrail.wrap_tool_execute(
            None, call=call, tool_def=None, args={}, handler=handler
        ))

        assert result == "done"
        # Counter unchanged
        assert guardrail._spike_count == 2
        assert handler.call_count == 1

    def test_file_modifying_resets_even_when_count_is_zero(self) -> None:
        """Resetting the counter when it's already zero is a no-op (no crash)."""
        guardrail = PostEditVerificationGuardrail()
        handler = _handler("ok")
        call = _Call("edit_file")

        result = asyncio.run(guardrail.wrap_tool_execute(
            None, call=call, tool_def=None, args={}, handler=handler
        ))

        assert result == "ok"
        assert guardrail._spike_count == 0
        assert guardrail._warned is False

    def test_non_file_non_spike_with_high_count_leaves_it_unchanged(self) -> None:
        """A non-target tool does not alter the spike count or warning flag."""
        guardrail = PostEditVerificationGuardrail()
        guardrail._spike_count = 3
        guardrail._warned = True
        handler = _handler("done")
        call = _Call("grep")

        asyncio.run(guardrail.wrap_tool_execute(
            None, call=call, tool_def=None, args={}, handler=handler
        ))

        assert guardrail._spike_count == 3
        assert guardrail._warned is True

    # ------------------------------------------------------------------
    # for_run — instance isolation
    # ------------------------------------------------------------------

    def test_for_run_returns_fresh_instance(self) -> None:
        """``for_run`` returns a new instance with zeroed counters."""
        guardrail = PostEditVerificationGuardrail()
        guardrail._spike_count = 3
        guardrail._warned = True

        fresh = asyncio.run(guardrail.for_run(None))

        assert fresh is not guardrail
        assert fresh._spike_count == 0
        assert fresh._warned is False

    def test_for_run_preserves_independent_counters(self) -> None:
        """The fresh instance's counter is independent of the original."""
        guardrail = PostEditVerificationGuardrail()
        guardrail._spike_count = 3

        fresh = asyncio.run(guardrail.for_run(None))
        fresh._spike_count = 1

        assert guardrail._spike_count == 3  # unchanged
        assert fresh._spike_count == 1

    # ------------------------------------------------------------------
    # Edge cases
    # ------------------------------------------------------------------

    def test_spike_with_zero_count_first_call_uses_handler(self) -> None:
        """A fresh guardrail's first spike_run call uses the handler normally."""
        guardrail = PostEditVerificationGuardrail()
        handler = _handler("ok")
        call = _Call("spike_run")

        result = asyncio.run(guardrail.wrap_tool_execute(
            None, call=call, tool_def=None, args={}, handler=handler
        ))

        assert result == "ok"
        assert guardrail._spike_count == 1

    def test_file_modifying_resets_warning_flag(self) -> None:
        """``_warned`` is reset to False by a file-modifying tool."""
        guardrail = PostEditVerificationGuardrail()
        guardrail._warned = True
        guardrail._spike_count = 2
        handler = _handler("done")

        asyncio.run(guardrail.wrap_tool_execute(
            None, call=_Call("edit_file"), tool_def=None, args={}, handler=handler
        ))

        assert guardrail._warned is False

    def test_warning_count_in_message_matches_threshold(self) -> None:
        """The warning message reports the correct consecutive count (3+)."""
        guardrail = PostEditVerificationGuardrail()
        guardrail._spike_count = 2
        call = _Call("spike_run")

        result = asyncio.run(guardrail.wrap_tool_execute(
            None, call=call, tool_def=None, args={}, handler=MagicMock()
        ))

        # The message should reference the threshold count, e.g. "3 consecutive"
        assert "3 consecutive" in result or f"{guardrail._THRESHOLD} consecutive" in result

    def test_model_retry_message_includes_implement_output(self) -> None:
        """The ModelRetry message clearly tells the agent to return its output."""
        guardrail = PostEditVerificationGuardrail()
        guardrail._spike_count = 3
        guardrail._warned = True
        call = _Call("spike_run")

        with pytest.raises(ModelRetry) as excinfo:
            asyncio.run(guardrail.wrap_tool_execute(
                None, call=call, tool_def=None, args={}, handler=MagicMock()
            ))

        msg = str(excinfo.value)
        assert "ImplementOutput" in msg
        assert "return your implementoutput now" in msg.lower()
