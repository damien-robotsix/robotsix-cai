"""Tests for per-scope token-usage accumulation on RunTranscript (issue #1286).

Verifies:
- Top-level AssistantMessage usage accumulates on RunTranscript.usage.
- Subagent AssistantMessage usage accumulates on SubAgentNode.usage.
- Parent and child usage remain independent (subagent isolation).
- Message ID deduplication: same message_id in same scope counted once.
- Parallel tool-calls fixture: three AssistantMessages with identical
  message_id and usage → scope usage counted once per scope.
- JSON round-trip: model_dump_json / model_validate_json preserves
  all per-scope usage int fields (cache_hit_rate is a property, not
  serialised, but recomputed on access after restore).

Uses unittest to match the project convention — pytest is not installed.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
)

from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk.types import (
    AssistantMessage,
    TextBlock,
    ToolUseBlock,
    UserMessage,
    ToolResultBlock,
)

from cai_lib.subagent import core
from cai_lib.subagent.core import SubAgent
from cai_lib.subagent.transcript import (
    RunTranscript,
    SubAgentNode,
    TokenUsage,
)
from tests._helpers import _mk_result, _mock_query


def _assistant(
    *,
    text: str = "ok",
    parent_tool_use_id: str | None = None,
    usage: dict | None = None,
    message_id: str | None = None,
    model: str = "claude-sonnet",
) -> AssistantMessage:
    """Build an AssistantMessage with optional usage and message_id."""
    return AssistantMessage(
        content=[TextBlock(text=text)],
        model=model,
        parent_tool_use_id=parent_tool_use_id,
        usage=usage,
        message_id=message_id,
    )


def _task(block_id: str, subagent_type: str = "general-purpose") -> ToolUseBlock:
    return ToolUseBlock(
        id=block_id,
        name="Task",
        input={"subagent_type": subagent_type, "prompt": "go"},
    )


class TestTokenUsageModel(unittest.TestCase):
    """Unit tests for the TokenUsage model itself."""

    def test_default_all_zero(self):
        u = TokenUsage()
        self.assertEqual(u.input_tokens, 0)
        self.assertEqual(u.output_tokens, 0)
        self.assertEqual(u.cache_read_input_tokens, 0)
        self.assertEqual(u.cache_creation_input_tokens, 0)

    def test_cache_hit_rate_none_when_denom_zero(self):
        u = TokenUsage()
        self.assertIsNone(u.cache_hit_rate)

    def test_cache_hit_rate_computed(self):
        u = TokenUsage(
            cache_read_input_tokens=80,
            cache_creation_input_tokens=10,
            input_tokens=10,
        )
        # 80 / (80 + 10 + 10) = 0.8
        self.assertAlmostEqual(u.cache_hit_rate, 0.8, places=4)

    def test_cache_hit_rate_rounded_to_4dp(self):
        u = TokenUsage(
            cache_read_input_tokens=1,
            cache_creation_input_tokens=0,
            input_tokens=2,  # denom=3, rate=0.3333...
        )
        self.assertEqual(u.cache_hit_rate, round(1 / 3, 4))

    def test_cache_hit_rate_not_serialised(self):
        """cache_hit_rate is a property — must not appear in model_dump."""
        u = TokenUsage(cache_read_input_tokens=50, input_tokens=50)
        d = u.model_dump()
        self.assertNotIn("cache_hit_rate", d)
        self.assertIn("cache_read_input_tokens", d)

    def test_json_round_trip(self):
        u = TokenUsage(input_tokens=10, output_tokens=5, cache_read_input_tokens=30, cache_creation_input_tokens=10)
        restored = TokenUsage.model_validate_json(u.model_dump_json())
        self.assertEqual(restored.input_tokens, 10)
        self.assertEqual(restored.output_tokens, 5)
        self.assertEqual(restored.cache_read_input_tokens, 30)
        self.assertEqual(restored.cache_creation_input_tokens, 10)
        # Property recomputed after restore.
        self.assertAlmostEqual(restored.cache_hit_rate, round(30 / 50, 4))


class TestTopLevelUsageAccumulation(unittest.TestCase):
    """Top-level AssistantMessages accumulate on RunTranscript.usage."""

    def test_single_message_accumulated(self):
        result_msg = _mk_result()
        msg = _assistant(
            usage={"input_tokens": 10, "output_tokens": 5,
                   "cache_read_input_tokens": 30, "cache_creation_input_tokens": 10},
            message_id="msg-1",
        )
        opts = ClaudeAgentOptions()
        agent = SubAgent(category="test", agent="test-agent", options=opts)
        with patch.object(core, "query", _mock_query(msg, result_msg)):
            rr = agent.run("hi")
        self.assertTrue(rr.ok)
        u = rr.transcript.usage
        self.assertEqual(u.input_tokens, 10)
        self.assertEqual(u.output_tokens, 5)
        self.assertEqual(u.cache_read_input_tokens, 30)
        self.assertEqual(u.cache_creation_input_tokens, 10)

    def test_multiple_messages_accumulated(self):
        """Two distinct messages with different IDs are both counted."""
        result_msg = _mk_result()
        msg1 = _assistant(
            usage={"input_tokens": 10, "output_tokens": 5},
            message_id="msg-1",
        )
        msg2 = _assistant(
            usage={"input_tokens": 20, "output_tokens": 3},
            message_id="msg-2",
        )
        opts = ClaudeAgentOptions()
        agent = SubAgent(category="test", agent="test-agent", options=opts)
        with patch.object(core, "query", _mock_query(msg1, msg2, result_msg)):
            rr = agent.run("hi")
        u = rr.transcript.usage
        self.assertEqual(u.input_tokens, 30)
        self.assertEqual(u.output_tokens, 8)

    def test_no_usage_field_skipped(self):
        """AssistantMessage with usage=None contributes zero tokens."""
        result_msg = _mk_result()
        msg = _assistant(usage=None, message_id="msg-1")
        opts = ClaudeAgentOptions()
        agent = SubAgent(category="test", agent="test-agent", options=opts)
        with patch.object(core, "query", _mock_query(msg, result_msg)):
            rr = agent.run("hi")
        u = rr.transcript.usage
        self.assertEqual(u.input_tokens, 0)
        self.assertEqual(u.output_tokens, 0)


class TestMessageIdDeduplication(unittest.TestCase):
    """Same message_id in the same scope is counted only once."""

    def test_duplicate_id_at_top_level_counted_once(self):
        """Three AssistantMessages with identical message_id → counted once."""
        result_msg = _mk_result()
        shared_id = "msg-shared"
        usage = {"input_tokens": 10, "output_tokens": 5}
        msg1 = _assistant(usage=usage, message_id=shared_id)
        msg2 = _assistant(usage=usage, message_id=shared_id)
        msg3 = _assistant(usage=usage, message_id=shared_id)
        opts = ClaudeAgentOptions()
        agent = SubAgent(category="test", agent="test-agent", options=opts)
        with patch.object(core, "query", _mock_query(msg1, msg2, msg3, result_msg)):
            rr = agent.run("hi")
        u = rr.transcript.usage
        # Counted once despite three messages with the same ID.
        self.assertEqual(u.input_tokens, 10)
        self.assertEqual(u.output_tokens, 5)

    def test_none_message_id_always_accumulated(self):
        """Messages with message_id=None have no dedup key — always counted."""
        result_msg = _mk_result()
        usage = {"input_tokens": 10, "output_tokens": 5}
        msg1 = _assistant(usage=usage, message_id=None)
        msg2 = _assistant(usage=usage, message_id=None)
        opts = ClaudeAgentOptions()
        agent = SubAgent(category="test", agent="test-agent", options=opts)
        with patch.object(core, "query", _mock_query(msg1, msg2, result_msg)):
            rr = agent.run("hi")
        u = rr.transcript.usage
        # Both counted (no ID to deduplicate on).
        self.assertEqual(u.input_tokens, 20)
        self.assertEqual(u.output_tokens, 10)


class TestSubagentIsolation(unittest.TestCase):
    """Parent and child usage are independent."""

    def _run_with_subagent(self):
        """Shared fixture: parent emits one top-level message and spawns a
        subagent; child emits two distinct messages inside the subagent."""
        outer_id = "tooluse_outer"
        result_msg = _mk_result()

        # Top-level: parent assistant message + Task spawn.
        msg_top = AssistantMessage(
            content=[
                TextBlock(text="parent think"),
                _task(outer_id),
            ],
            model="claude-opus",
            parent_tool_use_id=None,
            usage={"input_tokens": 100, "output_tokens": 20},
            message_id="top-msg-1",
        )
        # Inside subagent scope.
        msg_child_1 = _assistant(
            usage={"input_tokens": 30, "output_tokens": 10},
            parent_tool_use_id=outer_id,
            message_id="child-msg-1",
        )
        msg_child_2 = _assistant(
            usage={"input_tokens": 40, "output_tokens": 15},
            parent_tool_use_id=outer_id,
            message_id="child-msg-2",
        )
        # Back at top-level.
        msg_final = _assistant(
            usage={"input_tokens": 5, "output_tokens": 2},
            parent_tool_use_id=None,
            message_id="top-msg-2",
        )
        return (
            _mock_query(msg_top, msg_child_1, msg_child_2, msg_final, result_msg),
            outer_id,
        )

    def test_parent_usage_excludes_child_tokens(self):
        mock_q, outer_id = self._run_with_subagent()
        opts = ClaudeAgentOptions()
        agent = SubAgent(category="test", agent="test-agent", options=opts)
        with patch.object(core, "query", mock_q):
            rr = agent.run("hi")
        # Top-level: msg_top (100 in, 20 out) + msg_final (5 in, 2 out)
        top_u = rr.transcript.usage
        self.assertEqual(top_u.input_tokens, 105)
        self.assertEqual(top_u.output_tokens, 22)

    def test_child_usage_excludes_parent_tokens(self):
        mock_q, outer_id = self._run_with_subagent()
        opts = ClaudeAgentOptions()
        agent = SubAgent(category="test", agent="test-agent", options=opts)
        with patch.object(core, "query", mock_q):
            rr = agent.run("hi")
        # Find the SubAgentNode.
        transcript = rr.transcript
        outer_node = next(
            ev for ev in transcript.events
            if isinstance(ev, SubAgentNode) and ev.tool_use_id == outer_id
        )
        # Child: msg_child_1 (30 in, 10 out) + msg_child_2 (40 in, 15 out)
        child_u = outer_node.usage
        self.assertEqual(child_u.input_tokens, 70)
        self.assertEqual(child_u.output_tokens, 25)


class TestRoundTrip(unittest.TestCase):
    """JSON serialisation / deserialisation preserves per-scope usage."""

    def test_transcript_usage_round_trips(self):
        outer_id = "tooluse_rt"
        result_msg = _mk_result()
        msg_top = AssistantMessage(
            content=[TextBlock(text="hi"), _task(outer_id)],
            model="claude-opus",
            parent_tool_use_id=None,
            usage={"input_tokens": 50, "output_tokens": 10,
                   "cache_read_input_tokens": 200, "cache_creation_input_tokens": 50},
            message_id="rt-top",
        )
        msg_child = _assistant(
            usage={"input_tokens": 15, "output_tokens": 5},
            parent_tool_use_id=outer_id,
            message_id="rt-child",
        )
        opts = ClaudeAgentOptions()
        agent = SubAgent(category="test", agent="test-agent", options=opts)
        with patch.object(core, "query", _mock_query(msg_top, msg_child, result_msg)):
            rr = agent.run("hi")
        transcript = rr.transcript

        # Serialise (excluding ResultMessage which is an SDK dataclass).
        data = transcript.model_dump(exclude={"result"}, mode="json")
        restored = RunTranscript.model_validate(data)

        # Top-level usage preserved.
        self.assertEqual(restored.usage.input_tokens, 50)
        self.assertEqual(restored.usage.output_tokens, 10)
        self.assertEqual(restored.usage.cache_read_input_tokens, 200)
        self.assertEqual(restored.usage.cache_creation_input_tokens, 50)
        # cache_hit_rate recomputed from fields after restore.
        # 200 / (200 + 50 + 50) = 0.6667
        self.assertAlmostEqual(
            restored.usage.cache_hit_rate,
            round(200 / 300, 4),
        )

        # Child usage preserved inside the SubAgentNode.
        outer_node = next(
            ev for ev in restored.events
            if isinstance(ev, SubAgentNode) and ev.tool_use_id == outer_id
        )
        self.assertEqual(outer_node.usage.input_tokens, 15)
        self.assertEqual(outer_node.usage.output_tokens, 5)

    def test_model_dump_json_and_validate_json(self):
        outer_id = "tooluse_json"
        result_msg = _mk_result()
        msg_top = AssistantMessage(
            content=[TextBlock(text="go"), _task(outer_id)],
            model="claude-opus",
            parent_tool_use_id=None,
            usage={"input_tokens": 7, "output_tokens": 3},
            message_id="json-top",
        )
        opts = ClaudeAgentOptions()
        agent = SubAgent(category="test", agent="test-agent", options=opts)
        with patch.object(core, "query", _mock_query(msg_top, result_msg)):
            rr = agent.run("hi")
        transcript = rr.transcript

        json_blob = transcript.model_dump_json(exclude={"result"})
        restored = RunTranscript.model_validate_json(json_blob)
        self.assertEqual(restored.usage.input_tokens, 7)
        self.assertEqual(restored.usage.output_tokens, 3)


if __name__ == "__main__":
    unittest.main()
