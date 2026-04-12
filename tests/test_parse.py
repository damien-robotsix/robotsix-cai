"""Tests for parse.extract_tool_calls."""
import json
import sys
import os
import unittest

# Ensure the repo root is on the import path so `import parse` works
# regardless of how the test runner is invoked.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parse import extract_tool_calls


def _jl(*entries):
    """Serialise each entry to a JSONL line."""
    return [json.dumps(e) for e in entries]


def _assistant(tool_uses, usage=None):
    """Build a Claude Code assistant JSONL entry."""
    content = [{"type": "tool_use", "id": f"t{i}", "name": name, "input": {}}
               for i, name in enumerate(tool_uses)]
    msg = {"role": "assistant", "content": content}
    if usage:
        msg["usage"] = usage
    return {"type": "assistant", "message": msg}


def _tool_result(tool_name, is_error=False, text=""):
    """Build a Claude Code user JSONL entry carrying a tool_result block."""
    block = {"type": "tool_result", "tool_use_id": "t0", "content": text}
    if is_error:
        block["is_error"] = True
    return {"type": "user", "message": {"role": "user", "content": [block]}}


class TestExtractToolCalls(unittest.TestCase):

    def test_minimal_valid_session(self):
        lines = _jl(
            _assistant(["Read"], usage={"input_tokens": 100, "output_tokens": 50}),
        )
        result = extract_tool_calls(lines)
        self.assertEqual(result["tool_call_count"], 1)
        self.assertEqual(result["top_tools"], ["Read"])
        self.assertEqual(result["token_usage"]["input_tokens"], 100)
        self.assertEqual(result["token_usage"]["output_tokens"], 50)

    def test_empty_session(self):
        lines = ["", "   ", "\t"]
        result = extract_tool_calls(lines)
        self.assertEqual(result["tool_call_count"], 0)
        self.assertEqual(result["top_tools"], [])
        self.assertEqual(result["error_tools"], {})

    def test_error_tools_and_categories(self):
        # network_auth error
        lines = _jl(
            _assistant(["Bash"]),
            _tool_result("Bash", is_error=True, text="connection refused"),
        )
        result = extract_tool_calls(lines)
        self.assertEqual(result["error_tools"], {"Bash": 1})
        self.assertEqual(result["error_categories"]["network_auth"], 1)
        self.assertEqual(result["error_categories"]["controllable"], 0)

        # controllable error
        lines2 = _jl(
            _assistant(["Read"]),
            _tool_result("Read", is_error=True, text="file not found"),
        )
        result2 = extract_tool_calls(lines2)
        self.assertEqual(result2["error_categories"]["controllable"], 1)
        self.assertEqual(result2["error_categories"]["network_auth"], 0)

    def test_repeated_sequences(self):
        # 5 consecutive Read calls — run_length >= 3 triggers detection
        entries = [_assistant(["Read"]) for _ in range(5)]
        lines = _jl(*entries)
        result = extract_tool_calls(lines)
        self.assertTrue(len(result["repeated_sequences"]) >= 1)
        seq = result["repeated_sequences"][0]
        self.assertEqual(seq["tool"], "Read")
        self.assertEqual(seq["run_length"], 5)
        self.assertEqual(seq["start_index"], 0)

    def test_multi_file_aggregate(self):
        # Simulates concatenated lines from two "sessions"
        session1 = _jl(_assistant(["Read"]), _assistant(["Read"]))
        session2 = _jl(_assistant(["Grep"]), _assistant(["Grep"]), _assistant(["Grep"]))
        all_lines = session1 + session2
        result = extract_tool_calls(all_lines)
        self.assertEqual(result["tool_call_count"], 5)

    def test_malformed_jsonl_skipped(self):
        valid = _jl(_assistant(["Glob"]))[0]
        lines = ["not json", valid, "{bad"]
        result = extract_tool_calls(lines)
        self.assertEqual(result["tool_call_count"], 1)


if __name__ == "__main__":
    unittest.main()
