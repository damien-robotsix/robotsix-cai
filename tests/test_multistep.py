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
    def test_no_depth_label_returns_zero(self):
        issue = {"labels": [{"name": "auto-improve"}, {"name": "auto-improve:raised"}]}
        self.assertEqual(_issue_depth(issue), 0)

    def test_depth_label_returns_n(self):
        issue = {"labels": [{"name": "auto-improve"}, {"name": "depth:1"}]}
        self.assertEqual(_issue_depth(issue), 1)

    def test_depth_two(self):
        issue = {"labels": [{"name": "depth:2"}, {"name": "auto-improve:raised"}]}
        self.assertEqual(_issue_depth(issue), 2)

    def test_empty_labels(self):
        issue = {"labels": []}
        self.assertEqual(_issue_depth(issue), 0)

    def test_no_labels_key(self):
        issue = {}
        self.assertEqual(_issue_depth(issue), 0)

    def test_malformed_depth_label_ignored(self):
        issue = {"labels": [{"name": "depth:abc"}]}
        self.assertEqual(_issue_depth(issue), 0)


class TestCreateSubIssuesDepth(unittest.TestCase):
    @patch("cai_lib.actions.refine.link_sub_issue")
    @patch("cai_lib.actions.refine.create_issue")
    @patch("cai_lib.actions.refine._find_sub_issue", return_value=None)
    def test_depth_label_applied(self, mock_find, mock_create, mock_link):
        mock_create.return_value = {"number": 42, "id": 999, "html_url": "http://x"}
        steps = [{"step": 1, "title": "T", "body": "B"}]
        _create_sub_issues(steps, 10, "Parent", depth=1)
        labels = mock_create.call_args[0][2]
        self.assertIn("depth:1", labels)


class TestDepthGate(unittest.TestCase):
    @patch("cai_lib.actions.refine._run_claude_p")
    @patch("cai_lib.actions.refine.apply_transition")
    @patch("cai_lib.actions.refine._build_issue_block", return_value="issue text")
    def test_max_depth_injects_no_decompose(self, mock_build, mock_transition, mock_claude):
        """At max depth, user_message should instruct agent not to decompose."""
        mock_claude.return_value = MagicMock(
            returncode=0, stdout="## Refined Issue\nContent", stderr=""
        )
        with patch("cai_lib.actions.refine._run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            with patch("cai_lib.actions.refine.MAX_DECOMPOSITION_DEPTH", 2):
                issue = {
                    "number": 5, "title": "Test",
                    "labels": [{"name": "depth:2"}],
                    "body": "test body",
                }
                handle_refine(issue)
        call_kwargs = mock_claude.call_args
        input_msg = call_kwargs.kwargs.get("input") or call_kwargs[1].get("input", "")
        self.assertIn("Do NOT produce", input_msg)


if __name__ == "__main__":
    unittest.main()
