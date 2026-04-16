"""Tests for publish module."""
import sys
import os
import unittest

# Ensure the repo root is on the import path so `import publish` works
# regardless of how the test runner is invoked.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from publish import Finding, CHECK_WORKFLOWS_LABELS, LABELS_TO_DELETE  # noqa: E402


class TestCheckWorkflowsLabels(unittest.TestCase):

    def test_check_workflows_raised_not_in_labels(self):
        """check-workflows:raised must NOT appear in CHECK_WORKFLOWS_LABELS (retired label)."""
        label_names = [name for name, _, _ in CHECK_WORKFLOWS_LABELS]
        self.assertNotIn("check-workflows:raised", label_names)

    def test_check_workflows_in_labels(self):
        """check-workflows source tag must remain in CHECK_WORKFLOWS_LABELS."""
        label_names = [name for name, _, _ in CHECK_WORKFLOWS_LABELS]
        self.assertIn("check-workflows", label_names)

    def test_auto_improve_raised_in_check_workflows_labels(self):
        """auto-improve:raised must be in CHECK_WORKFLOWS_LABELS so new findings enter the FSM."""
        label_names = [name for name, _, _ in CHECK_WORKFLOWS_LABELS]
        self.assertIn("auto-improve:raised", label_names)

    def test_check_workflows_raised_in_labels_to_delete(self):
        """check-workflows:raised must be in LABELS_TO_DELETE so it gets cleaned up on publish runs."""
        self.assertIn("check-workflows:raised", LABELS_TO_DELETE)


if __name__ == "__main__":
    unittest.main()
