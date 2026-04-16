"""Tests for cai_lib.subprocess_utils — envelope parsing in _run_claude_p.

Focus: the stdout rewrite priority order when a claude -p invocation
returns a JSON envelope. The rules a gate-critical agent depends on:

  1. `structured_output` (from --json-schema constrained decoding) wins
     over the free-form `result` text, because the free-form text
     contains the model's reasoning and won't json.loads — this was the
     root cause of the #729 (cai-select) and #695 (cai-triage) failures.
  2. `error_max_structured_output_retries` subtype surfaces as an empty
     stdout + a dedicated log line so callers' emptiness check fires.
  3. Without a schema, the `result` text still wins (unchanged).
"""
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _envelope_list(**result_fields) -> str:
    """Build a claude-p --output-format json --verbose response body.

    The real CLI emits a JSON array of stream events; the final
    element has type=result and carries cost/structured-output data.
    """
    return json.dumps([
        {"type": "system"},
        {"type": "result", **result_fields},
    ])


class TestRunClaudePEnvelope(unittest.TestCase):
    """_run_claude_p rewrites proc.stdout based on envelope priority."""

    def _mock_run(self, stdout: str, returncode: int = 0, stderr: str = ""):
        return subprocess.CompletedProcess(
            args=["claude", "-p"], returncode=returncode,
            stdout=stdout, stderr=stderr,
        )

    @patch("cai_lib.subprocess_utils.log_cost")
    @patch("cai_lib.subprocess_utils._run")
    def test_structured_output_wins_over_result(self, mock_run, _mock_log):
        """When --json-schema succeeded, structured_output must override result text.

        This is the #729 / #695 regression: the model's reasoning lives
        in `result`, and the validated payload lives in `structured_output`.
        Callers use json.loads(stdout), so stdout must be the validated
        payload — not the prose the model produced.
        """
        from cai_lib.subprocess_utils import _run_claude_p

        validated = {"plan": "do X", "confidence": "HIGH",
                     "confidence_reason": "sound"}
        reasoning = "Routed **APPLY** (HIGH). Plan looks correct."
        mock_run.return_value = self._mock_run(_envelope_list(
            subtype="success",
            structured_output=validated,
            result=reasoning,
            total_cost_usd=0.1,
            usage={"input_tokens": 10, "output_tokens": 5},
        ))

        proc = _run_claude_p(
            ["claude", "-p", "--agent", "cai-select",
             "--json-schema", "{}"],
            category="plan.select", agent="cai-select",
        )

        # stdout must be the validated JSON payload, not the reasoning.
        self.assertEqual(json.loads(proc.stdout), validated)
        self.assertNotIn("Routed", proc.stdout)

    @patch("cai_lib.subprocess_utils.log_cost")
    @patch("cai_lib.subprocess_utils._run")
    def test_retries_exhausted_leaves_stdout_empty(self, mock_run, _mock_log):
        """error_max_structured_output_retries → empty stdout + diagnostic log."""
        from cai_lib.subprocess_utils import _run_claude_p

        mock_run.return_value = self._mock_run(_envelope_list(
            subtype="error_max_structured_output_retries",
            result="I couldn't match the schema sorry",
            total_cost_usd=0.2,
            usage={"input_tokens": 10, "output_tokens": 5},
        ))

        with patch("builtins.print") as mock_print:
            proc = _run_claude_p(
                ["claude", "-p", "--agent", "cai-triage",
                 "--json-schema", "{}"],
                category="triage", agent="cai-triage",
            )

        # stdout empty so the caller's "no output" guard triggers.
        self.assertEqual(proc.stdout, "")
        # A dedicated log line names the failure mode.
        printed = " ".join(str(c) for c in mock_print.call_args_list)
        self.assertIn("structured output retries exhausted", printed)

    @patch("cai_lib.subprocess_utils.log_cost")
    @patch("cai_lib.subprocess_utils._run")
    def test_result_text_used_when_no_schema(self, mock_run, _mock_log):
        """Without --json-schema the envelope has no structured_output; use result."""
        from cai_lib.subprocess_utils import _run_claude_p

        mock_run.return_value = self._mock_run(_envelope_list(
            subtype="success",
            result="plain agent output",
            total_cost_usd=0.05,
            usage={"input_tokens": 10, "output_tokens": 5},
        ))

        proc = _run_claude_p(
            ["claude", "-p", "--agent", "cai-plan"],
            category="plan.plan", agent="cai-plan",
        )

        self.assertEqual(proc.stdout, "plain agent output")

    @patch("cai_lib.subprocess_utils.log_cost")
    @patch("cai_lib.subprocess_utils._run")
    def test_structured_output_none_falls_through_to_result(self, mock_run, _mock_log):
        """structured_output: null must not be treated as present."""
        from cai_lib.subprocess_utils import _run_claude_p

        mock_run.return_value = self._mock_run(_envelope_list(
            subtype="success",
            structured_output=None,
            result="fallback text",
            total_cost_usd=0.05,
            usage={"input_tokens": 10, "output_tokens": 5},
        ))

        proc = _run_claude_p(
            ["claude", "-p", "--agent", "cai-plan"],
            category="plan.plan", agent="cai-plan",
        )

        self.assertEqual(proc.stdout, "fallback text")


if __name__ == "__main__":
    unittest.main()
