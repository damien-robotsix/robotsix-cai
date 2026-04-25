"""Tests for the typed transcript collector (issue #1280).

Synthesizes a fake :func:`claude_agent_sdk.query` stream and asserts
that :func:`cai_lib.subagent.core._collect_results` (driven via
:meth:`SubAgent.run`) builds the expected :class:`RunTranscript` —
nested ``SubAgentNode`` instances, a ``ToolResultEvent`` routed under
the right parent, the singular terminating ``ResultMessage`` on
``transcript.result``, and matching derived helpers
(``last_assistant_text``, ``subagent_counts``).

Uses :mod:`unittest` to match the rest of the suite — ``pytest`` is
not in ``pyproject.toml``'s dependencies.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from tests._helpers import _mock_query


def _mk_result(**fields) -> ResultMessage:
    return ResultMessage(
        subtype=fields.pop("subtype", "success"),
        duration_ms=fields.pop("duration_ms", 100),
        duration_api_ms=fields.pop("duration_api_ms", 50),
        is_error=fields.pop("is_error", False),
        num_turns=fields.pop("num_turns", 1),
        session_id=fields.pop("session_id", "sess-x"),
        total_cost_usd=fields.pop("total_cost_usd", 0.01),
        usage=fields.pop("usage", None),
        result=fields.pop("result", "ok"),
    )


class TestRunTranscriptCollection(unittest.TestCase):
    def test_nested_subagents_and_round_trip(self):
        from cai_lib.subagent import core
        from cai_lib.subagent.core import SubAgent
        from cai_lib.subagent.transcript import (
            AssistantTextEvent,
            RunTranscript,
            SubAgentNode,
            ToolResultEvent,
        )

        outer_id = "tooluse_outer"
        inner_id = "tooluse_inner"

        msg_top = AssistantMessage(
            content=[
                TextBlock(text="hello"),
                ToolUseBlock(
                    id=outer_id,
                    name="Task",
                    input={
                        "subagent_type": "general-purpose",
                        "prompt": "go",
                    },
                ),
            ],
            model="claude-opus",
            parent_tool_use_id=None,
        )

        msg_outer = AssistantMessage(
            content=[
                TextBlock(text="outer-think"),
                ToolUseBlock(
                    id=inner_id,
                    name="Task",
                    input={
                        "subagent_type": "cai-explore",
                        "prompt": "deeper",
                    },
                ),
            ],
            model="claude-sonnet",
            parent_tool_use_id=outer_id,
        )

        msg_inner = AssistantMessage(
            content=[TextBlock(text="inner-think")],
            model="claude-haiku",
            parent_tool_use_id=inner_id,
        )

        msg_user_result = UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id=inner_id, content="inner-result",
                ),
            ],
            parent_tool_use_id=outer_id,
        )

        msg_final = AssistantMessage(
            content=[TextBlock(text="done")],
            model="claude-opus",
            parent_tool_use_id=None,
        )

        result_msg = _mk_result()

        opts = ClaudeAgentOptions()
        agent = SubAgent(category="test", agent="test-agent", options=opts)

        with patch.object(
            core, "query",
            _mock_query(
                msg_top, msg_outer, msg_inner,
                msg_user_result, msg_final, result_msg,
            ),
        ):
            run_result = agent.run("hi")

        self.assertTrue(run_result.ok)
        transcript = run_result.transcript
        self.assertIsNotNone(transcript)

        # (a) Nesting: top-level events = 3 (assistant 'hello',
        # SubAgentNode, assistant 'done').
        self.assertEqual(len(transcript.events), 3)
        self.assertIsInstance(transcript.events[0], AssistantTextEvent)
        self.assertEqual(transcript.events[0].text, "hello")
        self.assertEqual(transcript.events[0].model, "claude-opus")
        self.assertIsInstance(transcript.events[1], SubAgentNode)
        self.assertEqual(
            transcript.events[1].subagent_type, "general-purpose")
        self.assertEqual(transcript.events[1].tool_use_id, outer_id)
        self.assertIsInstance(transcript.events[2], AssistantTextEvent)
        self.assertEqual(transcript.events[2].text, "done")

        outer_node = transcript.events[1]
        self.assertEqual(len(outer_node.events), 3)
        self.assertIsInstance(outer_node.events[0], AssistantTextEvent)
        self.assertEqual(outer_node.events[0].text, "outer-think")
        self.assertIsInstance(outer_node.events[1], SubAgentNode)
        inner_node = outer_node.events[1]
        self.assertEqual(inner_node.subagent_type, "cai-explore")
        self.assertEqual(inner_node.tool_use_id, inner_id)
        self.assertIsInstance(outer_node.events[2], ToolResultEvent)
        self.assertEqual(outer_node.events[2].tool_use_id, inner_id)
        self.assertEqual(outer_node.events[2].content, "inner-result")

        self.assertEqual(len(inner_node.events), 1)
        self.assertIsInstance(inner_node.events[0], AssistantTextEvent)
        self.assertEqual(inner_node.events[0].text, "inner-think")

        # (b) result is the singular terminating ResultMessage.
        self.assertIs(transcript.result, result_msg)

        # (c) recursive subagent_counts.
        self.assertEqual(
            transcript.subagent_counts,
            {"general-purpose": 1, "cai-explore": 1},
        )

        # (d) last_assistant_text is the final non-empty top-level
        # AssistantTextEvent's text.
        self.assertEqual(transcript.last_assistant_text, "done")
        self.assertEqual(transcript.parent_model, "claude-opus")

        # (e) JSON round-trip — drop the embedded ResultMessage which is
        # an SDK dataclass not registered with Pydantic. Events tree
        # alone round-trips cleanly through validate_json.
        data = transcript.model_dump(exclude={"result"}, mode="json")
        restored = RunTranscript.model_validate(data)
        self.assertEqual(
            restored.subagent_counts,
            {"general-purpose": 1, "cai-explore": 1},
        )
        self.assertEqual(restored.last_assistant_text, "done")

        # And model_dump_json() does not raise (with result excluded).
        json_blob = transcript.model_dump_json(exclude={"result"})
        self.assertIn("general-purpose", json_blob)
        self.assertIn("cai-explore", json_blob)
        self.assertIn("inner-result", json_blob)


if __name__ == "__main__":
    unittest.main()
