"""Tests for multi-step issue helpers in cai.py."""
import sys
import os
import unittest

# Ensure the repo root is on the import path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai import _parse_decomposition


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


if __name__ == "__main__":
    unittest.main()
