"""Tests for multi-step issue helpers in cai.py."""
import sys
import os
import unittest

# Ensure the repo root is on the import path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib import _parse_decomposition


class TestParseDecomposition(unittest.TestCase):
    """Tests for _parse_decomposition."""

    def test_well_formed_two_steps(self):
        text = (
            "Some preamble text.\n\n"
            "## Multi-Step Decomposition\n\n"
            "### Step 1: Add schema migration\n"
            "### Problem\n"
            "Need to add a new column.\n\n"
            "### Plan\n"
            "1. Create migration file\n"
            "2. Run migrate\n\n"
            "### Step 2: Update API endpoints\n"
            "### Problem\n"
            "API needs to expose the new field.\n\n"
            "### Plan\n"
            "1. Add field to serializer\n"
        )
        steps = _parse_decomposition(text)
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0]["step"], 1)
        self.assertEqual(steps[0]["title"], "Add schema migration")
        self.assertIn("new column", steps[0]["body"])
        self.assertEqual(steps[1]["step"], 2)
        self.assertEqual(steps[1]["title"], "Update API endpoints")
        self.assertIn("serializer", steps[1]["body"])

    def test_three_steps(self):
        text = (
            "## Multi-Step Decomposition\n\n"
            "### Step 1: First\n"
            "Body one.\n\n"
            "### Step 2: Second\n"
            "Body two.\n\n"
            "### Step 3: Third\n"
            "Body three.\n"
        )
        steps = _parse_decomposition(text)
        self.assertEqual(len(steps), 3)
        self.assertEqual(steps[0]["title"], "First")
        self.assertEqual(steps[1]["title"], "Second")
        self.assertEqual(steps[2]["title"], "Third")

    def test_no_marker_returns_empty(self):
        text = "## Refined Issue\n\nSome content here."
        steps = _parse_decomposition(text)
        self.assertEqual(steps, [])

    def test_empty_string_returns_empty(self):
        steps = _parse_decomposition("")
        self.assertEqual(steps, [])

    def test_single_step_returns_one(self):
        """A single step is parsed (caller decides minimum threshold)."""
        text = (
            "## Multi-Step Decomposition\n\n"
            "### Step 1: Only step\n"
            "Body of the only step.\n"
        )
        steps = _parse_decomposition(text)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["step"], 1)
        self.assertEqual(steps[0]["title"], "Only step")

    def test_steps_sorted_by_number(self):
        """Steps should be sorted even if out of order in input."""
        text = (
            "## Multi-Step Decomposition\n\n"
            "### Step 3: Third\n"
            "Body three.\n\n"
            "### Step 1: First\n"
            "Body one.\n\n"
            "### Step 2: Second\n"
            "Body two.\n"
        )
        steps = _parse_decomposition(text)
        self.assertEqual(len(steps), 3)
        self.assertEqual([s["step"] for s in steps], [1, 2, 3])

    def test_step_body_preserves_multiline(self):
        text = (
            "## Multi-Step Decomposition\n\n"
            "### Step 1: Complex step\n"
            "### Problem\n"
            "Line one.\n"
            "Line two.\n\n"
            "### Plan\n"
            "1. Do A\n"
            "2. Do B\n\n"
            "### Verification\n"
            "Run tests.\n"
        )
        steps = _parse_decomposition(text)
        self.assertEqual(len(steps), 1)
        self.assertIn("Line one.", steps[0]["body"])
        self.assertIn("Line two.", steps[0]["body"])
        self.assertIn("Do A", steps[0]["body"])
        self.assertIn("Run tests.", steps[0]["body"])

    def test_title_on_same_line_as_step_header(self):
        """Title is extracted from text after '### Step N: '."""
        text = (
            "## Multi-Step Decomposition\n\n"
            "### Step 1: Inline title\n"
            "Body text here.\n\n"
            "### Step 2: Another title\n"
            "Body two.\n"
        )
        steps = _parse_decomposition(text)
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0]["title"], "Inline title")
        self.assertEqual(steps[1]["title"], "Another title")


from unittest.mock import patch, MagicMock
from cai_lib.actions.refine import _issue_depth, _create_sub_issues, handle_refine


class TestIssueDepth(unittest.TestCase):
    @patch("cai_lib.actions.refine.get_parent_issue", return_value=None)
    def test_no_parent_returns_zero(self, mock_parent):
        self.assertEqual(_issue_depth(42), 0)

    @patch("cai_lib.actions.refine.get_parent_issue")
    def test_one_parent_returns_one(self, mock_parent):
        mock_parent.side_effect = [{"number": 5}, None]
        self.assertEqual(_issue_depth(42), 1)

    @patch("cai_lib.actions.refine.get_parent_issue")
    def test_two_parents_returns_two(self, mock_parent):
        mock_parent.side_effect = [{"number": 5}, {"number": 3}, None]
        self.assertEqual(_issue_depth(42), 2)

    @patch("cai_lib.actions.refine.get_parent_issue", return_value=None)
    def test_top_level_issue_returns_zero(self, mock_parent):
        self.assertEqual(_issue_depth(99), 0)


class TestCreateSubIssuesNoDepthLabel(unittest.TestCase):
    @patch("cai_lib.actions.refine.link_sub_issue")
    @patch("cai_lib.actions.refine.create_issue")
    @patch("cai_lib.actions.refine._find_sub_issue", return_value=None)
    def test_no_depth_label_applied(self, mock_find, mock_create, mock_link):
        mock_create.return_value = {"number": 42, "id": 999, "html_url": "http://x"}
        steps = [{"step": 1, "title": "T", "body": "B"}]
        _create_sub_issues(steps, 10, "Parent")
        labels = mock_create.call_args[0][2]
        self.assertFalse(any(l.startswith("depth:") for l in labels))


class TestSplitDepthGate(unittest.TestCase):
    """At max depth, cai-split's user_message instructs the agent not to
    emit a decomposition block. Decomposition responsibility moved from
    cai-refine to cai-split in the refine-split-architecture change.
    """

    @patch("cai_lib.actions.split.log_run")
    @patch("cai_lib.actions.split._run_claude_p")
    @patch("cai_lib.actions.split.fire_trigger")
    @patch("cai_lib.actions.split._build_issue_block", return_value="issue text")
    @patch("cai_lib.actions.split._issue_depth", return_value=2)
    def test_max_depth_injects_no_decompose(
        self, mock_depth, mock_build, mock_transition, mock_claude, mock_log_run,
    ):
        mock_claude.return_value = MagicMock(
            returncode=0,
            stdout="## Split Verdict\n\nVERDICT: ATOMIC\n\nConfidence: HIGH\n",
            stderr="",
        )
        with patch("cai_lib.actions.split.MAX_DECOMPOSITION_DEPTH", 2):
            from cai_lib.actions.split import handle_split
            issue = {
                "number": 5, "title": "Test",
                "labels": [{"name": "auto-improve:splitting"}],
                "body": "test body",
            }
            handle_split(issue)
        call_kwargs = mock_claude.call_args
        input_msg = call_kwargs.kwargs.get("input") or call_kwargs[1].get("input", "")
        self.assertIn("Do NOT", input_msg)
        self.assertIn("Multi-Step Decomposition", input_msg)
        # Regression guard: handle_split's success path always ends in a
        # log_run call, so the mock must see at least one invocation.
        mock_log_run.assert_called()
        for call in mock_log_run.call_args_list:
            args, kwargs = call
            self.assertEqual(args[0], "split")
            self.assertEqual(kwargs.get("issue"), 5)


if __name__ == "__main__":
    unittest.main()
