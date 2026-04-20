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


if __name__ == "__main__":
    unittest.main()
