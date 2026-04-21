"""Tests for cai_lib.subprocess_utils — ResultMessage parsing in _run_claude_p.

Focus: the stdout rewrite priority order when a ``claude -p`` invocation
returns a ResultMessage via the claude-agent-sdk ``query()`` async
iterator. The rules a gate-critical agent depends on:

  1. ``structured_output`` (from ``--json-schema`` constrained decoding)
     wins over the free-form ``result`` text, because the free-form text
     contains the model's reasoning and won't json.loads — this was the
     root cause of the #729 (cai-select) and #695 (cai-triage) failures.
  2. ``error_max_structured_output_retries`` subtype surfaces as an empty
     stdout + a dedicated log line so callers' emptiness check fires.
  3. Without a schema, the ``result`` text still wins (unchanged).
"""
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_agent_sdk.types import ResultMessage


def _mk_result(**fields) -> ResultMessage:
    """Build a ResultMessage with sane defaults for required fields."""
    return ResultMessage(
        subtype=fields.pop("subtype", "success"),
        duration_ms=fields.pop("duration_ms", 1),
        duration_api_ms=fields.pop("duration_api_ms", 1),
        is_error=fields.pop("is_error", False),
        num_turns=fields.pop("num_turns", 1),
        session_id=fields.pop("session_id", "s1"),
        total_cost_usd=fields.pop("total_cost_usd", 0.1),
        usage=fields.pop("usage", {"input_tokens": 10, "output_tokens": 5}),
        result=fields.pop("result", None),
        structured_output=fields.pop("structured_output", None),
    )


def _mock_query(*messages):
    """Return an async-generator replacement for cai_lib.subprocess_utils.query."""
    async def _gen(*, prompt, options=None, transport=None):
        for m in messages:
            yield m
    return _gen


class TestRunClaudePEnvelope(unittest.TestCase):
    """_run_claude_p rewrites proc.stdout based on ResultMessage priority."""

    @patch("cai_lib.subprocess_utils.log_cost")
    def test_structured_output_wins_over_result(self, _mock_log):
        """When --json-schema succeeded, structured_output must override result text.

        This is the #729 / #695 regression: the model's reasoning lives
        in ``result``, and the validated payload lives in
        ``structured_output``. Callers use ``json.loads(stdout)``, so
        stdout must be the validated payload — not the prose the model
        produced.
        """
        from cai_lib import subprocess_utils
        from cai_lib.subprocess_utils import _run_claude_p

        validated = {"plan": "do X", "confidence": "HIGH",
                     "confidence_reason": "sound"}
        reasoning = "Routed **APPLY** (HIGH). Plan looks correct."
        msg = _mk_result(
            structured_output=validated,
            result=reasoning,
        )
        with patch.object(subprocess_utils, "query", _mock_query(msg)):
            proc = _run_claude_p(
                ["claude", "-p", "--agent", "cai-select",
                 "--json-schema", "{}"],
                category="plan.select", agent="cai-select",
            )

        self.assertEqual(json.loads(proc.stdout), validated)
        self.assertNotIn("Routed", proc.stdout)

    @patch("cai_lib.subprocess_utils.log_cost")
    def test_retries_exhausted_leaves_stdout_empty(self, _mock_log):
        """error_max_structured_output_retries → empty stdout + diagnostic log."""
        from cai_lib import subprocess_utils
        from cai_lib.subprocess_utils import _run_claude_p

        msg = _mk_result(
            subtype="error_max_structured_output_retries",
            is_error=True,
            result="I couldn't match the schema sorry",
            total_cost_usd=0.2,
        )
        with patch.object(subprocess_utils, "query", _mock_query(msg)):
            with patch("builtins.print") as mock_print:
                proc = _run_claude_p(
                    ["claude", "-p", "--agent", "cai-triage",
                     "--json-schema", "{}"],
                    category="triage", agent="cai-triage",
                )

        self.assertEqual(proc.stdout, "")
        printed = " ".join(str(c) for c in mock_print.call_args_list)
        self.assertIn("structured output retries exhausted", printed)

    @patch("cai_lib.subprocess_utils.log_cost")
    def test_result_text_used_when_no_schema(self, _mock_log):
        """Without --json-schema the envelope has no structured_output; use result."""
        from cai_lib import subprocess_utils
        from cai_lib.subprocess_utils import _run_claude_p

        msg = _mk_result(result="plain agent output", total_cost_usd=0.05)
        with patch.object(subprocess_utils, "query", _mock_query(msg)):
            proc = _run_claude_p(
                ["claude", "-p", "--agent", "cai-plan"],
                category="plan.plan", agent="cai-plan",
            )

        self.assertEqual(proc.stdout, "plain agent output")

    @patch("cai_lib.subprocess_utils.log_cost")
    def test_structured_output_none_falls_through_to_result(self, _mock_log):
        """structured_output: null must not be treated as present."""
        from cai_lib import subprocess_utils
        from cai_lib.subprocess_utils import _run_claude_p

        msg = _mk_result(
            structured_output=None,
            result="fallback text",
            total_cost_usd=0.05,
        )
        with patch.object(subprocess_utils, "query", _mock_query(msg)):
            proc = _run_claude_p(
                ["claude", "-p", "--agent", "cai-plan"],
                category="plan.plan", agent="cai-plan",
            )

        self.assertEqual(proc.stdout, "fallback text")


class TestStderrEnrichment(unittest.TestCase):
    """Issue #1106: ``_run_claude_p`` must populate ``stderr`` with a
    diagnostic summary on every non-zero returncode path so the
    downstream implement handler has something to log. Before #1106
    both the ``is_error=True`` and ``no-ResultMessage`` branches set
    ``stderr=""``, which is why issue #910 produced five
    byte-identical ``result=subagent_failed exit=1`` rows."""

    @patch("cai_lib.subprocess_utils.log_cost")
    def test_is_error_populates_stderr_with_subtype(self, _mock_log):
        """is_error=True must surface sdk_subtype and is_error in stderr."""
        from cai_lib import subprocess_utils
        from cai_lib.subprocess_utils import _run_claude_p

        msg = _mk_result(
            subtype="error_max_turns",
            is_error=True,
            result="Agent exhausted max_turns=60 before producing a plan.",
            total_cost_usd=0.4,
        )
        with patch.object(subprocess_utils, "query", _mock_query(msg)):
            proc = _run_claude_p(
                ["claude", "-p", "--agent", "cai-implement"],
                category="implement", agent="cai-implement",
            )

        self.assertEqual(proc.returncode, 1)
        self.assertIn("sdk_subtype=error_max_turns", proc.stderr)
        self.assertIn("is_error=True", proc.stderr)
        self.assertIn("Agent exhausted max_turns", proc.stderr)

    @patch("cai_lib.subprocess_utils.log_cost")
    def test_is_error_without_result_text_still_has_summary(self, _mock_log):
        """is_error=True with result=None must still carry subtype/is_error."""
        from cai_lib import subprocess_utils
        from cai_lib.subprocess_utils import _run_claude_p

        msg = _mk_result(
            subtype="error_max_structured_output_retries",
            is_error=True,
            result=None,
            total_cost_usd=0.2,
        )
        with patch.object(subprocess_utils, "query", _mock_query(msg)):
            with patch("builtins.print"):
                proc = _run_claude_p(
                    ["claude", "-p", "--agent", "cai-triage",
                     "--json-schema", "{}"],
                    category="triage", agent="cai-triage",
                )

        self.assertEqual(proc.returncode, 1)
        self.assertIn(
            "sdk_subtype=error_max_structured_output_retries",
            proc.stderr,
        )
        self.assertIn("is_error=True", proc.stderr)

    @patch("cai_lib.subprocess_utils.log_cost")
    def test_success_leaves_stderr_empty(self, _mock_log):
        """returncode=0 must NOT leak a diagnostic summary into stderr."""
        from cai_lib import subprocess_utils
        from cai_lib.subprocess_utils import _run_claude_p

        msg = _mk_result(result="ok", total_cost_usd=0.05)
        with patch.object(subprocess_utils, "query", _mock_query(msg)):
            proc = _run_claude_p(
                ["claude", "-p", "--agent", "cai-plan"],
                category="plan.plan", agent="cai-plan",
            )

        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, "")

    def test_no_result_message_populates_stderr(self):
        """The no-ResultMessage fallback path must surface a diagnostic."""
        from cai_lib import subprocess_utils
        from cai_lib.subprocess_utils import _run_claude_p

        # No ResultMessage and no AssistantMessage — the empty-iterator case.
        async def _empty_gen(*, prompt, options=None, transport=None):
            if False:
                yield  # pragma: no cover

        with patch.object(subprocess_utils, "query", _empty_gen):
            with patch("builtins.print"):
                proc = _run_claude_p(
                    ["claude", "-p", "--agent", "cai-implement"],
                    category="implement", agent="cai-implement",
                )

        self.assertEqual(proc.returncode, 1)
        self.assertIn("no_ResultMessage", proc.stderr)


class TestCliStderrCapture(unittest.TestCase):
    """The SDK's transport only pipes the `claude -p` subprocess's stderr
    when ``ClaudeAgentOptions.stderr`` is set. Previously we never wired
    that callback, so when the CLI crashed the SDK raised
    ``ProcessError("Command failed with exit code 1", stderr="Check stderr
    output for details")`` — the literal placeholder. The real crash
    reason vanished into the parent's inherited stderr and we could not
    diagnose the intermittent failures breaking the cycle loop.

    These tests verify that ``_run_claude_p`` now attaches a stderr sink
    and surfaces the captured lines in both the logged message and the
    returned ``CompletedProcess.stderr``.
    """

    def test_exception_path_includes_captured_cli_stderr(self):
        """SDK exception → stderr field must carry captured CLI stderr tail."""
        from cai_lib import subprocess_utils
        from cai_lib.subprocess_utils import _run_claude_p

        cli_lines = [
            "node: fatal: unexpected end of stream on stdin",
            "    at /usr/lib/node_modules/@anthropic-ai/claude-code/cli.js:42",
        ]

        async def _fake_query(*, prompt, options=None, transport=None):
            # Simulate the SDK transport feeding stderr lines through the
            # callback before raising ProcessError the same way the real
            # transport does on a subprocess crash.
            sink = options.stderr
            if sink:
                for line in cli_lines:
                    sink(line)
            raise RuntimeError(
                "Fatal error in message reader: Command failed with exit code 1"
            )
            yield  # pragma: no cover — make it an async generator

        with patch.object(subprocess_utils, "query", _fake_query):
            with patch("builtins.print") as mock_print:
                proc = _run_claude_p(
                    ["claude", "-p", "--agent", "cai-implement"],
                    category="implement", agent="cai-implement",
                )

        self.assertEqual(proc.returncode, 1)
        self.assertIn("unexpected end of stream", proc.stderr)
        self.assertIn("cli.js:42", proc.stderr)
        self.assertIn("--- cli stderr ---", proc.stderr)
        # Log line must also mention cli_stderr=... so grepping the
        # wrapper's own log surfaces the real cause.
        printed = " ".join(str(c) for c in mock_print.call_args_list)
        self.assertIn("cli_stderr=", printed)
        self.assertIn("unexpected end of stream", printed)

    def test_exception_without_captured_stderr_falls_back(self):
        """When the CLI emitted no stderr, behaviour matches the pre-#1 contract."""
        from cai_lib import subprocess_utils
        from cai_lib.subprocess_utils import _run_claude_p

        async def _fake_query(*, prompt, options=None, transport=None):
            raise RuntimeError("boom")
            yield  # pragma: no cover

        with patch.object(subprocess_utils, "query", _fake_query):
            with patch("builtins.print"):
                proc = _run_claude_p(
                    ["claude", "-p", "--agent", "cai-implement"],
                    category="implement", agent="cai-implement",
                )

        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stderr, "boom")

    def test_stderr_capture_is_bounded(self):
        """The sink must cap at _CAPTURED_STDERR_MAX_LINES to avoid leaks."""
        from cai_lib.subprocess_utils import (
            _CAPTURED_STDERR_MAX_LINES,
            _captured_stderr_text,
            _make_stderr_sink,
        )

        buf: list[str] = []
        sink = _make_stderr_sink(buf)
        for i in range(_CAPTURED_STDERR_MAX_LINES * 3):
            sink(f"line {i}")

        self.assertEqual(len(buf), _CAPTURED_STDERR_MAX_LINES)
        # Early lines are preserved (bounded-append keeps the head —
        # matches the "first crash symptom wins" shape the transport
        # tends to produce).
        self.assertEqual(buf[0], "line 0")
        text = _captured_stderr_text(buf)
        self.assertTrue(text)


class TestSdkErrorSummary(unittest.TestCase):
    """Direct unit tests for the issue-#1106 summary helper."""

    def test_summary_with_subtype_and_result_text(self):
        from cai_lib.subprocess_utils import _sdk_error_summary

        class _R:
            subtype = "error_max_turns"
            is_error = True
            result = "hit the cap\nbailing out"

        s = _sdk_error_summary(_R())
        self.assertIn("sdk_subtype=error_max_turns", s)
        self.assertIn("is_error=True", s)
        self.assertIn("hit the cap", s)
        # The newline in result must be collapsed to a space by
        # the helper so the caller's log line stays single-line.
        self.assertNotIn("\n", s)

    def test_summary_with_missing_fields_defaults_to_none(self):
        from cai_lib.subprocess_utils import _sdk_error_summary

        class _R:  # no subtype, no is_error, no result
            pass

        s = _sdk_error_summary(_R())
        self.assertIn("sdk_subtype=none", s)
        self.assertIn("is_error=False", s)


if __name__ == "__main__":
    unittest.main()
