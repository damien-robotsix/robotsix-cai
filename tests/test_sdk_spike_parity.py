"""Side-by-side parity test for the SDK spike (issue #1226).

Asserts that porting one handler off the ``_run_claude_p`` argv facade
onto a direct ``ClaudeAgentOptions`` + ``run_subagent`` call emits an
identical cost-row payload (modulo the per-call dynamic ``ts`` /
``session_id`` / ``host`` fields) and an identical
:class:`subprocess.CompletedProcess` triple
(``returncode`` / ``stdout`` / ``stderr``).

Uses ``unittest`` to match the rest of the tree — ``pytest`` is not in
``pyproject.toml``'s dependencies.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk.types import ResultMessage


def _mk_result(**fields) -> ResultMessage:
    """Build a ResultMessage with deterministic defaults."""
    return ResultMessage(
        subtype=fields.pop("subtype", "success"),
        duration_ms=fields.pop("duration_ms", 1234),
        duration_api_ms=fields.pop("duration_api_ms", 999),
        is_error=fields.pop("is_error", False),
        num_turns=fields.pop("num_turns", 3),
        session_id=fields.pop("session_id", "sess-fixed"),
        total_cost_usd=fields.pop("total_cost_usd", 0.1234),
        usage=fields.pop("usage", {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 200,
            "cache_read_input_tokens": 800,
        }),
        result=fields.pop("result", "ok"),
        structured_output=fields.pop("structured_output", None),
        model_usage=fields.pop("model_usage", {
            "claude-sonnet-4": {
                "inputTokens": 100,
                "outputTokens": 50,
                "cacheReadInputTokens": 800,
                "cacheCreationInputTokens": 200,
                "costUSD": 0.1234,
            },
        }),
    )


def _mock_query(*messages):
    """Async-iterator replacement for ``cai_lib.subagent.core.query``."""
    async def _gen(*, prompt, options=None, transport=None):
        for m in messages:
            yield m
    return _gen


_VOLATILE_KEYS = {"ts", "session_id", "host"}


def _strip_volatile(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in _VOLATILE_KEYS}


class TestSdkSpikeParity(unittest.TestCase):
    """``run_subagent`` emits the same cost-row payload as ``_run_claude_p``."""

    def test_cost_rows_match_modulo_volatile_fields(self):
        import cai_lib.cai_subagent as cai_subagent_mod
        from cai_lib.subagent import _run_claude_p, core, legacy
        from cai_lib.cai_subagent import run_subagent

        prompt = "## test prompt\n\nfor parity check"
        captured: list[dict] = []

        def _capture(row: dict) -> None:
            captured.append(dict(row))

        msg_a = _mk_result()
        with patch.object(core, "query", _mock_query(msg_a)), \
             patch.object(legacy, "log_cost", side_effect=_capture):
            _run_claude_p(
                ["claude", "-p", "--agent", "cai-confirm"],
                category="confirm",
                agent="cai-confirm",
                input=prompt,
            )

        msg_b = _mk_result()
        with patch.object(core, "query", _mock_query(msg_b)), \
             patch.object(cai_subagent_mod, "log_cost", side_effect=_capture):
            opts = ClaudeAgentOptions(extra_args={"agent": "cai-confirm"})
            run_subagent(
                prompt,
                opts,
                category="confirm",
                agent="cai-confirm",
            )

        self.assertEqual(len(captured), 2,
                         "expected one log_cost call per code path")
        facade_row, native_row = captured
        self.assertEqual(
            _strip_volatile(facade_row),
            _strip_volatile(native_row),
        )

    def test_returncode_stdout_stderr_match_on_success(self):
        from cai_lib.subagent import _run_claude_p, core, legacy, run_subagent

        prompt = "## another fixture"

        msg_a = _mk_result(result="payload-text")
        with patch.object(core, "query", _mock_query(msg_a)), \
             patch.object(legacy, "log_cost"):
            facade = _run_claude_p(
                ["claude", "-p", "--agent", "cai-confirm"],
                category="confirm",
                agent="cai-confirm",
                input=prompt,
            )

        msg_b = _mk_result(result="payload-text")
        with patch.object(core, "query", _mock_query(msg_b)):
            opts = ClaudeAgentOptions(extra_args={"agent": "cai-confirm"})
            native = run_subagent(
                prompt,
                opts,
                category="confirm",
                agent="cai-confirm",
            )

        self.assertEqual(facade.returncode, native.returncode)
        self.assertEqual(facade.stdout, native.stdout)
        self.assertEqual(facade.stderr, native.stderr)

    def test_returncode_stdout_stderr_match_on_error(self):
        from cai_lib.subagent import _run_claude_p, core, legacy, run_subagent

        prompt = "## error fixture"

        msg_a = _mk_result(
            subtype="error_max_turns",
            is_error=True,
            result="exhausted",
        )
        with patch.object(core, "query", _mock_query(msg_a)), \
             patch.object(legacy, "log_cost"), \
             patch("builtins.print"):
            facade = _run_claude_p(
                ["claude", "-p", "--agent", "cai-confirm"],
                category="confirm",
                agent="cai-confirm",
                input=prompt,
            )

        msg_b = _mk_result(
            subtype="error_max_turns",
            is_error=True,
            result="exhausted",
        )
        with patch.object(core, "query", _mock_query(msg_b)):
            opts = ClaudeAgentOptions(extra_args={"agent": "cai-confirm"})
            native = run_subagent(
                prompt,
                opts,
                category="confirm",
                agent="cai-confirm",
            )

        self.assertEqual(facade.returncode, 1)
        self.assertEqual(native.returncode, 1)
        self.assertEqual(facade.stdout, native.stdout)
        self.assertEqual(facade.stderr, native.stderr)


if __name__ == "__main__":
    unittest.main()
