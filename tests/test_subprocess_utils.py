"""Tests for cai_lib.subagent — ResultMessage parsing in _run_claude_p.

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
    """Return an async-generator replacement for cai_lib.subagent.core.query."""
    async def _gen(*, prompt, options=None, transport=None):
        for m in messages:
            yield m
    return _gen


class TestRunClaudePEnvelope(unittest.TestCase):
    """_run_claude_p rewrites proc.stdout based on ResultMessage priority."""

    @patch("cai_lib.cai_subagent.log_cost")
    def test_structured_output_wins_over_result(self, _mock_log):
        """When --json-schema succeeded, structured_output must override result text.

        This is the #729 / #695 regression: the model's reasoning lives
        in ``result``, and the validated payload lives in
        ``structured_output``. Callers use ``json.loads(stdout)``, so
        stdout must be the validated payload — not the prose the model
        produced.
        """
        from cai_lib.claude_argv import _run_claude_p
        from cai_lib.subagent import core

        validated = {"plan": "do X", "confidence": "HIGH",
                     "confidence_reason": "sound"}
        reasoning = "Routed **APPLY** (HIGH). Plan looks correct."
        msg = _mk_result(
            structured_output=validated,
            result=reasoning,
        )
        with patch.object(core, "query", _mock_query(msg)):
            proc = _run_claude_p(
                ["claude", "-p", "--agent", "cai-select",
                 "--json-schema", "{}"],
                category="plan.select", agent="cai-select",
            )

        self.assertEqual(json.loads(proc.stdout), validated)
        self.assertNotIn("Routed", proc.stdout)

    @patch("cai_lib.cai_subagent.log_cost")
    def test_retries_exhausted_leaves_stdout_empty(self, _mock_log):
        """error_max_structured_output_retries → empty stdout + diagnostic log."""
        from cai_lib.claude_argv import _run_claude_p
        from cai_lib.subagent import core

        msg = _mk_result(
            subtype="error_max_structured_output_retries",
            is_error=True,
            result="I couldn't match the schema sorry",
            total_cost_usd=0.2,
        )
        with self.assertLogs("cai_lib.subagent.core", level="WARNING") as cm:
            with patch.object(core, "query", _mock_query(msg)):
                proc = _run_claude_p(
                    ["claude", "-p", "--agent", "cai-triage",
                     "--json-schema", "{}"],
                    category="triage", agent="cai-triage",
                )

        self.assertEqual(proc.stdout, "")
        log_text = "\n".join(cm.output)
        self.assertIn("structured output retries exhausted", log_text)

    @patch("cai_lib.cai_subagent.log_cost")
    def test_result_text_used_when_no_schema(self, _mock_log):
        """Without --json-schema the envelope has no structured_output; use result."""
        from cai_lib.claude_argv import _run_claude_p
        from cai_lib.subagent import core

        msg = _mk_result(result="plain agent output", total_cost_usd=0.05)
        with patch.object(core, "query", _mock_query(msg)):
            proc = _run_claude_p(
                ["claude", "-p", "--agent", "cai-plan"],
                category="plan.plan", agent="cai-plan",
            )

        self.assertEqual(proc.stdout, "plain agent output")

    @patch("cai_lib.cai_subagent.log_cost")
    def test_structured_output_none_falls_through_to_result(self, _mock_log):
        """structured_output: null must not be treated as present."""
        from cai_lib.claude_argv import _run_claude_p
        from cai_lib.subagent import core

        msg = _mk_result(
            structured_output=None,
            result="fallback text",
            total_cost_usd=0.05,
        )
        with patch.object(core, "query", _mock_query(msg)):
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

    @patch("cai_lib.cai_subagent.log_cost")
    def test_is_error_populates_stderr_with_subtype(self, _mock_log):
        """is_error=True must surface sdk_subtype and is_error in stderr."""
        from cai_lib.claude_argv import _run_claude_p
        from cai_lib.subagent import core

        msg = _mk_result(
            subtype="error_max_turns",
            is_error=True,
            result="Agent exhausted max_turns=60 before producing a plan.",
            total_cost_usd=0.4,
        )
        with patch.object(core, "query", _mock_query(msg)):
            proc = _run_claude_p(
                ["claude", "-p", "--agent", "cai-implement"],
                category="implement", agent="cai-implement",
            )

        self.assertEqual(proc.returncode, 1)
        self.assertIn("sdk_subtype=error_max_turns", proc.stderr)
        self.assertIn("is_error=True", proc.stderr)
        self.assertIn("Agent exhausted max_turns", proc.stderr)

    @patch("cai_lib.cai_subagent.log_cost")
    def test_is_error_without_result_text_still_has_summary(self, _mock_log):
        """is_error=True with result=None must still carry subtype/is_error."""
        from cai_lib.claude_argv import _run_claude_p
        from cai_lib.subagent import core

        msg = _mk_result(
            subtype="error_max_structured_output_retries",
            is_error=True,
            result=None,
            total_cost_usd=0.2,
        )
        with patch.object(core, "query", _mock_query(msg)):
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

    @patch("cai_lib.cai_subagent.log_cost")
    def test_success_leaves_stderr_empty(self, _mock_log):
        """returncode=0 must NOT leak a diagnostic summary into stderr."""
        from cai_lib.claude_argv import _run_claude_p
        from cai_lib.subagent import core

        msg = _mk_result(result="ok", total_cost_usd=0.05)
        with patch.object(core, "query", _mock_query(msg)):
            proc = _run_claude_p(
                ["claude", "-p", "--agent", "cai-plan"],
                category="plan.plan", agent="cai-plan",
            )

        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, "")

    def test_no_result_message_populates_stderr(self):
        """The no-ResultMessage fallback path must surface a diagnostic."""
        from cai_lib.claude_argv import _run_claude_p
        from cai_lib.subagent import core

        # No ResultMessage and no AssistantMessage — the empty-iterator case.
        async def _empty_gen(*, prompt, options=None, transport=None):
            if False:
                yield  # pragma: no cover

        with patch.object(core, "query", _empty_gen):
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
        from cai_lib.claude_argv import _run_claude_p
        from cai_lib.subagent import core

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

        with self.assertLogs("cai_lib.subagent.core", level="WARNING") as cm:
            with patch.object(core, "query", _fake_query):
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
        log_text = "\n".join(cm.output)
        self.assertIn("cli_stderr=", log_text)
        self.assertIn("unexpected end of stream", log_text)

    def test_exception_without_captured_stderr_falls_back(self):
        """When the CLI emitted no stderr, behaviour matches the pre-#1 contract."""
        from cai_lib.claude_argv import _run_claude_p
        from cai_lib.subagent import core

        async def _fake_query(*, prompt, options=None, transport=None):
            raise RuntimeError("boom")
            yield  # pragma: no cover

        with patch.object(core, "query", _fake_query):
            with patch("builtins.print"):
                proc = _run_claude_p(
                    ["claude", "-p", "--agent", "cai-implement"],
                    category="implement", agent="cai-implement",
                )

        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stderr, "boom")

    def test_stderr_capture_is_bounded(self):
        """The sink must cap at _CAPTURED_STDERR_MAX_LINES to avoid leaks."""
        from cai_lib.subagent.stderr_sink import (
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
        from cai_lib.subagent.errors import _sdk_error_summary

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
        from cai_lib.subagent.errors import _sdk_error_summary

        class _R:  # no subtype, no is_error, no result
            pass

        s = _sdk_error_summary(_R())
        self.assertIn("sdk_subtype=none", s)
        self.assertIn("is_error=False", s)


class TestCostCommentKwargsBackwardsCompat(unittest.TestCase):
    """Issue #1168: ``target_kind`` / ``target_number`` must default to
    ``None`` so every existing call site (27+) that never passes them
    keeps working byte-for-byte identical to pre-#1168 behaviour."""

    @patch("cai_lib.cai_subagent.log_cost")
    def test_omitting_kwargs_does_not_change_returncode_or_stdout(self, _mock_log):
        from cai_lib.claude_argv import _run_claude_p
        from cai_lib.subagent import core

        msg = _mk_result(result="unchanged", total_cost_usd=0.01)
        with patch.object(core, "query", _mock_query(msg)):
            proc = _run_claude_p(
                ["claude", "-p", "--agent", "cai-plan"],
                category="plan.plan", agent="cai-plan",
            )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "unchanged")
        self.assertEqual(proc.stderr, "")

    @patch("cai_lib.cai_subagent.log_cost")
    def test_kwargs_accept_issue_and_pr_values(self, _mock_log):
        from cai_lib.claude_argv import _run_claude_p
        from cai_lib.subagent import core

        msg = _mk_result(result="ok", total_cost_usd=0.01)
        with patch.object(core, "query", _mock_query(msg)), \
             patch("cai_lib.github._post_issue_comment", return_value=True), \
             patch("cai_lib.github._post_pr_comment", return_value=True):
            proc1 = _run_claude_p(
                ["claude", "-p", "--agent", "cai-plan"],
                category="plan.plan", agent="cai-plan",
                target_kind="issue", target_number=1,
            )
            proc2 = _run_claude_p(
                ["claude", "-p", "--agent", "cai-merge"],
                category="merge", agent="cai-merge",
                target_kind="pr", target_number=2,
            )
        self.assertEqual(proc1.returncode, 0)
        self.assertEqual(proc2.returncode, 0)


class TestExtraTargetCostComment(unittest.TestCase):
    """``cai revise`` / ``cai merge`` mirror their cost-attribution
    comment onto the linked issue so humans scanning the issue see
    every agent's spend, not just the PR's. ``extra_target_kind`` /
    ``extra_target_number`` drive that second post.
    """

    @patch("cai_lib.cai_subagent.log_cost")
    def test_extra_target_posts_to_both_pr_and_issue(self, _mock_log):
        from cai_lib.claude_argv import _run_claude_p
        from cai_lib.subagent import core

        msg = _mk_result(result="ok", total_cost_usd=0.02)
        with patch.object(core, "query", _mock_query(msg)), \
             patch(
                "cai_lib.github._post_issue_comment", return_value=True,
             ) as mock_issue_post, \
             patch(
                "cai_lib.github._post_pr_comment", return_value=True,
             ) as mock_pr_post:
            proc = _run_claude_p(
                ["claude", "-p", "--agent", "cai-merge"],
                category="merge", agent="cai-merge",
                target_kind="pr", target_number=42,
                extra_target_kind="issue", extra_target_number=99,
            )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(mock_pr_post.call_count, 1)
        self.assertEqual(mock_issue_post.call_count, 1)
        # Targets mirror the kwargs (PR #42 and issue #99).
        pr_args, _ = mock_pr_post.call_args
        issue_args, _ = mock_issue_post.call_args
        self.assertEqual(pr_args[0], 42)
        self.assertEqual(issue_args[0], 99)

    @patch("cai_lib.cai_subagent.log_cost")
    def test_extra_target_omitted_posts_only_to_primary(self, _mock_log):
        from cai_lib.claude_argv import _run_claude_p
        from cai_lib.subagent import core

        msg = _mk_result(result="ok", total_cost_usd=0.02)
        with patch.object(core, "query", _mock_query(msg)), \
             patch(
                "cai_lib.github._post_issue_comment", return_value=True,
             ) as mock_issue_post, \
             patch(
                "cai_lib.github._post_pr_comment", return_value=True,
             ) as mock_pr_post:
            proc = _run_claude_p(
                ["claude", "-p", "--agent", "cai-plan"],
                category="plan.plan", agent="cai-plan",
                target_kind="issue", target_number=7,
            )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(mock_pr_post.call_count, 0)
        self.assertEqual(mock_issue_post.call_count, 1)


class TestFsmStateStamping(unittest.TestCase):
    """Issue #1203: ``_run_claude_p`` must stamp the current FSM state
    (set by the dispatcher via ``set_current_fsm_state``) onto each
    cost-log row under the optional ``fsm_state`` key, and omit the
    key entirely when the contextvar is unset."""

    def test_fsm_state_stamped_when_contextvar_set(self):
        from cai_lib.claude_argv import _run_claude_p
        import cai_lib.cai_subagent as cai_subagent_mod
        from cai_lib.subagent import core
        from cai_lib.fsm_state import set_current_fsm_state

        captured: list[dict] = []

        def _fake_log_cost(row: dict) -> None:
            captured.append(dict(row))

        msg = _mk_result(result="ok", total_cost_usd=0.05)
        with patch.object(core, "query", _mock_query(msg)), \
             patch.object(cai_subagent_mod, "log_cost", _fake_log_cost):
            with set_current_fsm_state("REFINING"):
                _run_claude_p(
                    ["claude", "-p", "--agent", "cai-refine"],
                    category="refine", agent="cai-refine",
                )

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].get("fsm_state"), "REFINING")

    def test_fsm_state_omitted_when_contextvar_unset(self):
        from cai_lib.claude_argv import _run_claude_p
        import cai_lib.cai_subagent as cai_subagent_mod
        from cai_lib.subagent import core

        captured: list[dict] = []

        def _fake_log_cost(row: dict) -> None:
            captured.append(dict(row))

        msg = _mk_result(result="ok", total_cost_usd=0.05)
        with patch.object(core, "query", _mock_query(msg)), \
             patch.object(cai_subagent_mod, "log_cost", _fake_log_cost):
            _run_claude_p(
                ["claude", "-p", "--agent", "cai-audit-health"],
                category="audit", agent="cai-audit-health",
            )

        self.assertEqual(len(captured), 1)
        self.assertNotIn("fsm_state", captured[0])

    def test_fsm_state_reset_after_block_exits(self):
        """Nested/sequential blocks must restore the prior value on exit."""
        from cai_lib.claude_argv import _run_claude_p
        import cai_lib.cai_subagent as cai_subagent_mod
        from cai_lib.subagent import core
        from cai_lib.fsm_state import set_current_fsm_state

        captured: list[dict] = []

        def _fake_log_cost(row: dict) -> None:
            captured.append(dict(row))

        msg = _mk_result(result="ok", total_cost_usd=0.01)
        with patch.object(core, "query", _mock_query(msg)), \
             patch.object(cai_subagent_mod, "log_cost", _fake_log_cost):
            with set_current_fsm_state("PLANNING"):
                _run_claude_p(
                    ["claude", "-p", "--agent", "cai-plan"],
                    category="plan.plan", agent="cai-plan",
                )
            # Outside the block, the stamp must be cleared.
            _run_claude_p(
                ["claude", "-p", "--agent", "cai-plan"],
                category="plan.plan", agent="cai-plan",
            )

        self.assertEqual(len(captured), 2)
        self.assertEqual(captured[0].get("fsm_state"), "PLANNING")
        self.assertNotIn("fsm_state", captured[1])


class TestCacheHitRateAnnotation(unittest.TestCase):
    """Issue #1205: ``_run_claude_p`` must pre-compute a single
    authoritative ``cache_hit_rate`` value on each cost-log row
    (aggregate) and a ``cacheHitRate`` value inside each ``models[m]``
    entry (per-model). Rows with no cache/input tokens observed must
    omit the field so legacy rows stay byte-identical."""

    def test_cache_hit_rate_set_when_tokens_present(self):
        from cai_lib.claude_argv import _run_claude_p
        import cai_lib.cai_subagent as cai_subagent_mod
        from cai_lib.subagent import core

        captured: list[dict] = []

        def _fake_log_cost(row: dict) -> None:
            captured.append(dict(row))

        # 50 cache_read + 25 cache_create + 25 input → denom=100, hit=0.5.
        usage = {
            "input_tokens": 25,
            "output_tokens": 10,
            "cache_creation_input_tokens": 25,
            "cache_read_input_tokens": 50,
        }
        msg = _mk_result(
            usage=usage, result="ok", total_cost_usd=0.01,
        )
        with patch.object(core, "query", _mock_query(msg)), \
             patch.object(cai_subagent_mod, "log_cost", _fake_log_cost):
            _run_claude_p(
                ["claude", "-p", "--agent", "cai-plan"],
                category="plan.plan", agent="cai-plan",
            )

        self.assertEqual(len(captured), 1)
        self.assertIn("cache_hit_rate", captured[0])
        self.assertAlmostEqual(captured[0]["cache_hit_rate"], 0.5, places=4)

    def test_cache_hit_rate_omitted_when_denominator_zero(self):
        from cai_lib.claude_argv import _run_claude_p
        import cai_lib.cai_subagent as cai_subagent_mod
        from cai_lib.subagent import core

        captured: list[dict] = []

        def _fake_log_cost(row: dict) -> None:
            captured.append(dict(row))

        # No input_tokens, no cache_* tokens — denom=0 → key must be
        # absent so legacy callers (audit/cost.py, cost-report) can
        # still rely on ``r.get("cache_hit_rate") is None`` as the
        # "missing" signal.
        usage = {"output_tokens": 5}
        msg = _mk_result(
            usage=usage, result="ok", total_cost_usd=0.01,
        )
        with patch.object(core, "query", _mock_query(msg)), \
             patch.object(cai_subagent_mod, "log_cost", _fake_log_cost):
            _run_claude_p(
                ["claude", "-p", "--agent", "cai-plan"],
                category="plan.plan", agent="cai-plan",
            )

        self.assertEqual(len(captured), 1)
        self.assertNotIn("cache_hit_rate", captured[0])

    def test_per_model_cache_hit_rate_stamped(self):
        """Each ``models[m]`` entry gets a ``cacheHitRate`` when its
        per-model cache/input tokens are non-zero. Entries whose
        denominator is zero are skipped (no key written)."""
        from cai_lib.claude_argv import _run_claude_p
        import cai_lib.cai_subagent as cai_subagent_mod
        from cai_lib.subagent import core

        captured: list[dict] = []

        def _fake_log_cost(row: dict) -> None:
            captured.append(dict(row))

        usage = {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 30,
            "cache_creation_input_tokens": 10,
        }
        msg = _mk_result(
            usage=usage, result="ok", total_cost_usd=0.01,
        )

        # Patch ``query`` to yield a message whose ResultMessage.model_usage
        # carries two model entries: one with tokens, one with zero
        # denom so we can assert the per-model skip rule.
        async def _gen(*, prompt, options=None, transport=None):
            # Attach model_usage on the instance so the parser picks
            # it up (ResultMessage may not accept it via constructor).
            msg.model_usage = {
                "claude-sonnet-4-6": {
                    "inputTokens": 10,
                    "cacheReadInputTokens": 30,
                    "cacheCreationInputTokens": 10,
                    "outputTokens": 5,
                },
                "claude-haiku-4-5-20251001": {
                    "inputTokens": 0,
                    "cacheReadInputTokens": 0,
                    "cacheCreationInputTokens": 0,
                    "outputTokens": 0,
                },
            }
            yield msg

        with patch.object(core, "query", _gen), \
             patch.object(cai_subagent_mod, "log_cost", _fake_log_cost):
            _run_claude_p(
                ["claude", "-p", "--agent", "cai-plan"],
                category="plan.plan", agent="cai-plan",
            )

        self.assertEqual(len(captured), 1)
        models = captured[0].get("models") or {}
        sonnet = models.get("claude-sonnet-4-6") or {}
        haiku = models.get("claude-haiku-4-5-20251001") or {}
        # 30 / (30 + 10 + 10) = 0.6
        self.assertIn("cacheHitRate", sonnet)
        self.assertAlmostEqual(sonnet["cacheHitRate"], 0.6, places=4)
        # Zero-denom model must be skipped (no key added).
        self.assertNotIn("cacheHitRate", haiku)


class TestFixAttemptCountStamping(unittest.TestCase):
    """Issue #1204: ``_run_claude_p`` must stamp the caller-provided
    ``fix_attempt_count`` onto each cost-log row under the optional
    ``fix_attempt_count`` key, and omit the key entirely when the
    kwarg is unset (default) so pre-#1204 rows stay byte-identical.

    Mirrors the conditional-stamp pattern used by ``TestFsmStateStamping``
    above (issue #1203). Explicitly covers the ``fix_attempt_count=0``
    first-attempt case to guard against a future ``if fix_attempt_count:``
    truthiness-check regression — zero must be stamped.
    """

    def test_omitting_fix_attempt_count_leaves_row_unchanged(self):
        from cai_lib.claude_argv import _run_claude_p
        import cai_lib.cai_subagent as cai_subagent_mod
        from cai_lib.subagent import core

        captured: list[dict] = []

        def _fake_log_cost(row: dict) -> None:
            captured.append(dict(row))

        msg = _mk_result(result="ok", total_cost_usd=0.05)
        with patch.object(core, "query", _mock_query(msg)), \
             patch.object(cai_subagent_mod, "log_cost", _fake_log_cost):
            _run_claude_p(
                ["claude", "-p", "--agent", "cai-plan"],
                category="plan.plan", agent="cai-plan",
            )

        self.assertEqual(len(captured), 1)
        self.assertNotIn("fix_attempt_count", captured[0])

    def test_passing_fix_attempt_count_stamps_row(self):
        from cai_lib.claude_argv import _run_claude_p
        import cai_lib.cai_subagent as cai_subagent_mod
        from cai_lib.subagent import core

        captured: list[dict] = []

        def _fake_log_cost(row: dict) -> None:
            captured.append(dict(row))

        msg = _mk_result(result="ok", total_cost_usd=0.05)
        with patch.object(core, "query", _mock_query(msg)), \
             patch.object(cai_subagent_mod, "log_cost", _fake_log_cost):
            # First attempt: zero must land in the row (is-not-None check).
            _run_claude_p(
                ["claude", "-p", "--agent", "cai-implement"],
                category="implement", agent="cai-implement",
                fix_attempt_count=0,
            )
            # Retry: non-zero stamped as-is.
            _run_claude_p(
                ["claude", "-p", "--agent", "cai-implement"],
                category="implement", agent="cai-implement",
                fix_attempt_count=2,
            )

        self.assertEqual(len(captured), 2)
        self.assertIn("fix_attempt_count", captured[0])
        self.assertEqual(captured[0]["fix_attempt_count"], 0)
        self.assertIn("fix_attempt_count", captured[1])
        self.assertEqual(captured[1]["fix_attempt_count"], 2)


class TestModuleAndScopeFilesStamping(unittest.TestCase):
    """Issue #1206: ``_run_claude_p`` must stamp caller-supplied
    ``module`` and ``scope_files`` kwargs onto the cost-log row so
    downstream cost tooling can group spend by module (audit runs)
    or declared file scope (implement runs). Both keys must be
    omitted when the kwargs are unset, preserving pre-#1206 row
    shape; ``scope_files`` is capped at the first 10 paths."""

    def test_module_and_scope_files_stamped_when_provided(self):
        from cai_lib.claude_argv import _run_claude_p
        import cai_lib.cai_subagent as cai_subagent_mod
        from cai_lib.subagent import core

        captured: list[dict] = []

        def _fake_log_cost(row: dict) -> None:
            captured.append(dict(row))

        msg = _mk_result(result="ok", total_cost_usd=0.05)
        with patch.object(core, "query", _mock_query(msg)), \
             patch.object(cai_subagent_mod, "log_cost", _fake_log_cost):
            _run_claude_p(
                ["claude", "-p", "--agent", "cai-audit-health"],
                category="audit", agent="cai-audit-health",
                module="audit",
                scope_files=["a.py", "b.py"],
            )

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].get("module"), "audit")
        self.assertEqual(captured[0].get("scope_files"), ["a.py", "b.py"])

    def test_module_and_scope_files_omitted_when_unset(self):
        from cai_lib.claude_argv import _run_claude_p
        import cai_lib.cai_subagent as cai_subagent_mod
        from cai_lib.subagent import core

        captured: list[dict] = []

        def _fake_log_cost(row: dict) -> None:
            captured.append(dict(row))

        msg = _mk_result(result="ok", total_cost_usd=0.05)
        with patch.object(core, "query", _mock_query(msg)), \
             patch.object(cai_subagent_mod, "log_cost", _fake_log_cost):
            _run_claude_p(
                ["claude", "-p", "--agent", "cai-refine"],
                category="refine", agent="cai-refine",
            )

        self.assertEqual(len(captured), 1)
        self.assertNotIn("module", captured[0])
        self.assertNotIn("scope_files", captured[0])

    def test_scope_files_truncated_to_ten(self):
        from cai_lib.claude_argv import _run_claude_p
        import cai_lib.cai_subagent as cai_subagent_mod
        from cai_lib.subagent import core

        captured: list[dict] = []

        def _fake_log_cost(row: dict) -> None:
            captured.append(dict(row))

        many = [f"file_{i}.py" for i in range(15)]
        msg = _mk_result(result="ok", total_cost_usd=0.05)
        with patch.object(core, "query", _mock_query(msg)), \
             patch.object(cai_subagent_mod, "log_cost", _fake_log_cost):
            _run_claude_p(
                ["claude", "-p", "--agent", "cai-implement"],
                category="implement", agent="cai-implement",
                scope_files=many,
            )

        self.assertEqual(len(captured), 1)
        self.assertEqual(len(captured[0]["scope_files"]), 10)
        self.assertEqual(captured[0]["scope_files"], many[:10])


if __name__ == "__main__":
    unittest.main()
